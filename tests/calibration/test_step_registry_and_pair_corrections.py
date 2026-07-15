"""
test_step_registry_and_pair_corrections.py — Regression tests for two
recently-restored calibration features:

1. The content-addressed STEP registry (Prisma/data/_system/steps_registry.json)
   populated by DataStore.__init__'s migration, and queried by step_id.

2. The Fit All endpoint regenerating filaments/pair_corrections.json — a
   behavior that was silently lost in an earlier consolidation and has now
   been re-wired into POST /api/profiles/fit-all via
   fitting.compute_and_save_pair_corrections.

Every test uses a fresh tmp_path so there is no cross-test state.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_access import DataStore


@pytest.fixture(autouse=True)
def _legacy_measured_source():
    # Pair-correction registry tests use synthetic samples without sidecars; the
    # legacy path stays callable (doc 32). Production default is the sidecar.
    from fitting.fitting import use_measured_source
    with use_measured_source("legacy"):
        yield


# ── helpers ───────────────────────────────────────────────────────────────────

def _seed_minimal_data_root(tmp_path: Path) -> DataStore:
    """Create the minimum tree DataStore needs, with an empty filament registry."""
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _pair_sample(
    sample_id: str,
    *,
    variable: str = "bambu-red",
    fixed: list[str] | None = None,
    fixed_thicknesses: list[float] | None = None,
    status: str = "processed",
    swatches: list[tuple[int, float, tuple[float, float, float]]] | None = None,
    fit_exclude: bool = False,
    excluded_swatches: list[int] | None = None,
):
    if swatches is None:
        swatches = [(0, 0.16, (0.8, 0.6, 0.4))]
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
    return SimpleNamespace(
        sample_id=sample_id,
        processing_status=status,
        filaments=SimpleNamespace(variable=variable, fixed=fixed_ids),
        fit_exclude=fit_exclude,
        excluded_swatches=excluded_swatches or [],
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=[d for _, d, _ in swatches],
            fixed_thicknesses_mm=fixed_mm,
        ),
        roles=roles,
        measurements=SimpleNamespace(
            swatches=[
                SimpleNamespace(
                    swatch_index=idx,
                    nominal_thickness_mm=d,
                    R_linear=rgb[0],
                    G_linear=rgb[1],
                    B_linear=rgb[2],
                )
                for idx, d, rgb in swatches
            ]
        ),
    )


def _write_legacy_step_file(store: DataStore, filename: str) -> Path:
    """Create a placeholder .step file in the legacy step directory.

    The file contents are irrelevant — the migration only reads the filename
    to derive geometry via the STEP filename parser.
    """
    store.legacy_step_dir.mkdir(parents=True, exist_ok=True)
    path = store.legacy_step_dir / filename
    path.write_text("ISO-10303-21;\n", encoding="utf-8")
    return path


# ── STEP registry migration ───────────────────────────────────────────────────

class TestStepRegistryMigration:
    def test_legacy_step_file_produces_canonical_record(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step",
        )

        # Re-instantiate so the migration runs against the seeded legacy file.
        store = DataStore(tmp_path)
        records = store.list_step_records()

        assert len(records) == 1
        record = records[0]
        assert record.step_id.startswith("step_")
        assert record.layer_count == 1
        assert record.layer_height_mm == pytest.approx(0.08)
        assert record.variable_thicknesses_mm == [0.16, 0.24, 0.32, 0.40, 0.48, 0.56, 0.64, 0.72]
        assert record.artifact_exists is True

    def test_step_id_is_stable_across_boots(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "2L_v-0.16-0.28-0.36-0.52-0.68-0.92-1.24-1.72_f-0.20_lh0.08.step",
        )
        first_store = DataStore(tmp_path)
        first_records = first_store.list_step_records()

        second_store = DataStore(tmp_path)
        second_records = second_store.list_step_records()

        assert [r.step_id for r in first_records] == [r.step_id for r in second_records]

    def test_migration_is_idempotent_on_canonical_data(self, tmp_path):
        """Second boot against already-canonical data must not rewrite the
        registry, bundles, or sample JSONs. This is the contract that keeps
        the working tree clean across server restarts."""
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step",
        )

        # First boot — legitimate migration, writes expected.
        DataStore(tmp_path)
        registry_path = tmp_path / "_system" / "steps_registry.json"
        assert registry_path.exists()
        snapshot = registry_path.read_bytes()
        mtime_before = registry_path.stat().st_mtime_ns

        # Second boot against already-canonical data — must be a no-op.
        DataStore(tmp_path)

        assert registry_path.read_bytes() == snapshot, \
            "Registry bytes changed on a no-op boot — migration is not idempotent"
        assert registry_path.stat().st_mtime_ns == mtime_before, \
            "Registry mtime changed on a no-op boot — migration rewrote the file"

    def test_persisted_registry_contains_no_absolute_paths(self, tmp_path):
        """artifact_path and artifact_exists are machine-specific and must be
        recomputed at read time, never persisted."""
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step",
        )
        DataStore(tmp_path)

        registry_path = tmp_path / "_system" / "steps_registry.json"
        payload = json.loads(registry_path.read_text(encoding="utf-8"))

        for entry in payload["steps"]:
            assert "artifact_path" not in entry
            assert "artifact_exists" not in entry

    def test_ensure_step_artifact_does_not_rewrite_registry(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)
        record = store._step_record_from_components(
            layer_count=1,
            layer_height_mm=0.10,
            variable_thicknesses_mm=[0.10, 0.20],
            fixed_thicknesses_mm=[],
        )
        store.save_steps_registry({record.step_id: record.model_dump()})
        registry_path = tmp_path / "_system" / "steps_registry.json"
        snapshot = registry_path.read_bytes()
        mtime_before = registry_path.stat().st_mtime_ns

        ensured = store.ensure_step_artifact(record.step_id)

        assert ensured.artifact_exists is True
        assert registry_path.read_bytes() == snapshot
        assert registry_path.stat().st_mtime_ns == mtime_before

    def test_4l_fixed_thickness_order_is_part_of_identity(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)
        variable = [0.20, 0.36, 0.52, 0.68, 0.84, 1.00, 1.16, 1.32]

        bottom_to_top = store._step_record_from_components(
            layer_count=4,
            layer_height_mm=0.20,
            variable_thicknesses_mm=variable,
            fixed_thicknesses_mm=[0.20, 0.40, 0.40],
        )
        alternate_order = store._step_record_from_components(
            layer_count=4,
            layer_height_mm=0.20,
            variable_thicknesses_mm=variable,
            fixed_thicknesses_mm=[0.40, 0.40, 0.20],
        )

        assert bottom_to_top.file_name.endswith("_f-0.20-0.40-0.40_lh0.20.step")
        assert alternate_order.file_name.endswith("_f-0.40-0.40-0.20_lh0.20.step")
        assert bottom_to_top.step_id != alternate_order.step_id


# ── STEP lookups by step_id ───────────────────────────────────────────────────

class TestStepLookupByStepId:
    def test_find_step_record_by_step_id(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step",
        )
        store = DataStore(tmp_path)
        record = store.list_step_records()[0]

        found = store.find_step_record(step_id=record.step_id)

        assert found is not None
        assert found.step_id == record.step_id

    def test_find_step_record_by_legacy_filename(self, tmp_path):
        """Legacy ingress: passing an old filename must still resolve, via
        source_filenames bookkeeping."""
        store = _seed_minimal_data_root(tmp_path)
        _write_legacy_step_file(
            store,
            "1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step",
        )
        store = DataStore(tmp_path)

        found = store.find_step_record(
            step_file="1L_v-0.16-0.24-0.32-0.40-0.48-0.56-0.64-0.72_lh0.08.step"
        )

        assert found is not None
        assert found.layer_count == 1

    def test_find_step_record_returns_none_for_unknown(self, tmp_path):
        store = _seed_minimal_data_root(tmp_path)

        assert store.find_step_record(step_id="step_does_not_exist") is None
        assert store.find_step_record(step_file="nothing.step") is None


# ── Pair corrections generation ───────────────────────────────────────────────

class TestFitAllGeneratesPairCorrections:
    def test_compute_and_save_pair_corrections_writes_file(self, tmp_path, monkeypatch):
        """compute_and_save_pair_corrections must write pair_corrections.json
        next to the profiles directory, using whatever corrections
        fitter_core.compute_pair_corrections returns.

        We stub fitter_core to keep this test independent of the real fitter's
        sample-data requirements; the contract under test is the wiring layer,
        not the math.
        """
        store = _seed_minimal_data_root(tmp_path)
        profiles_dir = tmp_path / "filaments" / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        stub_corrections = {
            "bambu-basic-red|bambu-basic-blue@0.48": {
                "overlay": "bambu-basic-red",
                "bases": [["bambu-basic-blue", 0.48]],
                "knots_mm": [0.0, 0.16, 0.32],
                "C_r": [1.0, 1.1, 1.0],
                "C_g": [1.0, 1.0, 1.0],
                "C_b": [1.0, 0.95, 1.0],
            }
        }

        import fitting.fitter_core as fitter_core
        monkeypatch.setattr(
            fitter_core, "compute_pair_corrections",
            lambda registry, profiles: stub_corrections,
        )

        from fitting import fitting as fitting_module
        result = fitting_module.compute_and_save_pair_corrections(store, profiles_dir)

        expected_path = profiles_dir.parent / "pair_corrections.json"
        assert expected_path.exists(), "pair_corrections.json was not written"
        payload = json.loads(expected_path.read_text(encoding="utf-8"))
        assert "bambu-basic-red|bambu-basic-blue@0.48" in payload
        assert result["n_pairs"] == 1
        assert Path(result["path"]) == expected_path

    def test_build_registry_skips_unprocessed_and_excluded_samples(self, tmp_path, monkeypatch):
        """The registry builder must drop samples that aren't fully processed
        and samples in the hard-coded exclusion list — the fitter only wants
        usable data."""
        store = _seed_minimal_data_root(tmp_path)

        # Build a minimal Sample-like stub with the attributes the function reads.
        class _Swatch:
            def __init__(self, idx, r, g, b, d):
                self.swatch_index = idx
                self.R_linear = r
                self.G_linear = g
                self.B_linear = b
                self.nominal_thickness_mm = d

        class _Measurements:
            def __init__(self):
                self.swatches = [_Swatch(0, 0.8, 0.6, 0.4, 0.16)]

        class _StripDef:
            def __init__(self):
                self.variable_thicknesses_mm = [0.16]
                self.fixed_thicknesses_mm = []

        class _Filaments:
            def __init__(self, variable, fixed=None):
                self.variable = variable
                self.fixed = fixed or []

        class _Sample:
            def __init__(self, sample_id, status, variable_fid, excluded=False):
                self.sample_id = sample_id
                self.processing_status = status
                self.filaments = _Filaments(variable_fid)
                self.measurements = _Measurements()
                self.strip_definition = _StripDef()
                self.roles = [{
                    "role_index": 1,
                    "role_label": "LR_01",
                    "role_kind": "variable",
                    "filament_id": variable_fid,
                    "fixed_thickness_mm": None,
                }]

        samples = [
            _Sample("exp-001", "processed", "bambu-red"),
            _Sample("exp-002", "pending", "bambu-red"),          # skipped — unprocessed
            _Sample("exp-EXCLUDED", "processed", "bambu-red"),   # skipped — excluded
        ]
        monkeypatch.setattr(store, "list_samples", lambda: samples)

        from fitting import fitting as fitting_module
        monkeypatch.setattr(
            fitting_module, "EXCLUDED_EXPERIMENTS", {"exp-EXCLUDED"},
        )

        registry = fitting_module._build_pair_corrections_registry(store)

        assert set(registry.keys()) == {"bambu-red"}
        assert len(registry["bambu-red"]) == 1
        assert registry["bambu-red"][0]["sample_id"] == "exp-001"

    def test_build_registry_honors_sample_and_filament_exclusions(self, tmp_path, monkeypatch):
        """Pair corrections must use the same sample membership policy as the
        spline profile fit: explicit sample exclusions plus registry-derived
        filament exclusions for variable or fixed roles."""
        store = _seed_minimal_data_root(tmp_path)
        samples = [
            _pair_sample("exp-ok"),
            _pair_sample("exp-sample-excluded"),
            _pair_sample("exp-bad-variable", variable="bad-filament"),
            _pair_sample("exp-bad-fixed", fixed=["bad-filament"], fixed_thicknesses=[0.2]),
        ]
        monkeypatch.setattr(store, "list_samples", lambda: samples)
        monkeypatch.setattr(
            store,
            "list_filaments",
            lambda: [SimpleNamespace(filament_id="bad-filament", exclude_from_model=True)],
        )

        from fitting import fitting as fitting_module

        registry = fitting_module._build_pair_corrections_registry(
            store,
            excluded_samples={"exp-sample-excluded"},
        )

        assert set(registry.keys()) == {"bambu-red"}
        assert [entry["sample_id"] for entry in registry["bambu-red"]] == ["exp-ok"]

    def test_build_registry_honors_live_sample_fit_controls_without_caller_maps(self, tmp_path, monkeypatch):
        """Direct pair-correction calls should still honor live sample-owned
        fit controls, even if the server wrapper did not precollect maps."""
        store = _seed_minimal_data_root(tmp_path)
        samples = [
            _pair_sample("exp-fit-exclude", fit_exclude=True),
            _pair_sample(
                "exp-live-swatch-exclude",
                swatches=[
                    (0, 0.16, (0.8, 0.6, 0.4)),
                    (1, 0.32, (0.7, 0.5, 0.3)),
                ],
                excluded_swatches=[0],
            ),
        ]
        monkeypatch.setattr(store, "list_samples", lambda: samples)

        from fitting import fitting as fitting_module

        registry = fitting_module._build_pair_corrections_registry(store)

        assert [entry["sample_id"] for entry in registry["bambu-red"]] == ["exp-live-swatch-exclude"]
        assert registry["bambu-red"][0]["swatches"] == [
            {"d": 0.32, "measured_T": [0.7, 0.5, 0.3]}
        ]

    def test_build_registry_honors_swatch_exclusions(self, tmp_path, monkeypatch):
        """Excluded swatches must not enter pair correction data; a strip with
        all swatches excluded contributes nothing."""
        store = _seed_minimal_data_root(tmp_path)
        samples = [
            _pair_sample(
                "exp-partial",
                swatches=[
                    (0, 0.16, (0.8, 0.6, 0.4)),
                    (1, 0.32, (0.7, 0.5, 0.3)),
                ],
            ),
            _pair_sample(
                "exp-all",
                swatches=[
                    (0, 0.16, (0.8, 0.6, 0.4)),
                    (1, 0.32, (0.7, 0.5, 0.3)),
                ],
            ),
        ]
        monkeypatch.setattr(store, "list_samples", lambda: samples)

        from fitting import fitting as fitting_module

        registry = fitting_module._build_pair_corrections_registry(
            store,
            excluded_swatches={"exp-partial": {0}, "exp-all": {0, 1}},
        )

        assert [entry["sample_id"] for entry in registry["bambu-red"]] == ["exp-partial"]
        assert registry["bambu-red"][0]["swatches"] == [
            {"d": 0.32, "measured_T": [0.7, 0.5, 0.3]}
        ]

    def test_compute_pair_corrections_supports_package_style_import(self, tmp_path):
        """Package-style direct callers should not require Prisma/calibration
        on sys.path for the local fitter_core import."""
        project_root = Path(__file__).resolve().parents[2]
        profiles_dir = tmp_path / "filaments" / "profiles"
        profiles_dir.mkdir(parents=True)
        code = f"""
from pathlib import Path
from Prisma.calibration.fitting import fitting

class Store:
    def list_samples(self):
        return []
    def list_filaments(self):
        return []

result = fitting.compute_and_save_pair_corrections(Store(), Path({json.dumps(str(profiles_dir))}))
assert result["n_pairs"] == 0
"""
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
