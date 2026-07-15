"""Path setup for Prisma generator tests."""
import os
import json
import hashlib
import math
import shutil
import tempfile
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _PROJECT_ROOT / "Prisma" / "generator"
_PRISMA_DIR = _PROJECT_ROOT / "Prisma"

# Make generator modules importable
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

_TEST_RUNTIME = tempfile.TemporaryDirectory(
    prefix="prisma-generator-tests-",
    ignore_cleanup_errors=True,
)
_TEST_RUNTIME_ROOT = Path(_TEST_RUNTIME.name)
_TEST_LIBRARY = _TEST_RUNTIME_ROOT / "Published Test Library"


def _build_published_test_library() -> None:
    bundle_path = _PRISMA_DIR / "lib" / "photo_stack_model" / "bundles" / "runtime_bundle.json"
    live = json.loads(bundle_path.read_text(encoding="utf-8"))
    (_TEST_LIBRARY / "filaments").mkdir(parents=True)
    profiles = _TEST_LIBRARY / "filaments" / "profiles"
    profiles.mkdir()
    registry = {}
    floor = live["model"]["floor"]
    for filament_id, curve in live["model"]["curves"].items():
        # The deployment bundle may contain only one fitted Photo Stack row for
        # a filament.  Generator algorithm tests still need a mathematically
        # valid historical-spline fixture, so derive a deterministic monotonic
        # curve from that row instead of pretending its observations are spline
        # knots.
        attenuation = curve[0]
        knots_mm = [round(0.08 * index, 2) for index in range(26)]

        def _transmission(channel: int, key: str) -> list[float]:
            coefficient = max(float(attenuation[key]), 1e-6)
            return [
                float(floor[channel] + (1.0 - floor[channel]) * math.exp(-coefficient * depth))
                for depth in knots_mm
            ]

        profile = {
            "filament_id": filament_id,
            "model": "spline",
            "schema_version": 1,
            "knots_mm": knots_mm,
            "T_r": _transmission(0, "od_r"),
            "T_g": _transmission(1, "od_g"),
            "T_b": _transmission(2, "od_b"),
        }
        (profiles / f"{filament_id}.json").write_text(json.dumps(profile), encoding="utf-8")
        digest = hashlib.sha256(filament_id.encode("utf-8")).hexdigest()[:6].upper()
        is_white = "white" in filament_id.lower()
        registry[filament_id] = {
            "display_name": filament_id.replace("-", " ").title(),
            "manufacturer": filament_id.split("-", 1)[0].title(),
            "color_name": filament_id.replace("-", " ").title(),
            "material": "PLA",
            "hex": "#FFFFFF" if is_white else f"#{digest}",
            "white_cap_eligible": is_white,
            "special_roles": ["black"] if "black" in filament_id.lower() else [],
            "exclude_from_model": False,
            "generation_available": True,
        }
    (_TEST_LIBRARY / "filaments" / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    (_TEST_LIBRARY / "filaments" / "pair_corrections.json").write_text(
        json.dumps({"pairs": {}}), encoding="utf-8"
    )

    published_run = _TEST_LIBRARY / "filaments" / "photo_stack_models" / "published-v2"
    published_run.mkdir(parents=True)
    classifier = live.get("filament_classification") or {
        "schema": "prisma_photo_stack_model_white_classifier_v1",
        "mode": "legacy_token_white",
        "source": "legacy_token_white",
        "classifier_version": "legacy_token_white_v1",
        "model_white_filament_ids": [],
        "model_white_snapshot_hash": None,
    }
    deployment = {
        "schema": "prisma_photo_stack_v2_deployment_bundle",
        "schema_version": 1,
        "artifact_role": "published_model_library",
        "model_family": live.get("model_family", "photo_stack"),
        "model_version": live.get("model_version", "v2"),
        "runtime_constants_version": live["runtime_constants_version"],
        "fingerprint": live["fingerprint"],
        "filament_classification": classifier,
        "model": live["model"],
    }
    (published_run / "runtime_bundle.json").write_text(json.dumps(deployment), encoding="utf-8")
    (published_run / "correction_layer.json").write_text(
        json.dumps(
            {
                "schema": "prisma_photo_stack_v2_correction",
                "schema_version": 1,
                "correction_layer_version": "generator-test-empty",
                "base_model_name": "photo_stack_v2",
                "training_rows": [],
                "training_row_count": 0,
                "parameters": {},
            }
        ),
        encoding="utf-8",
    )
    (_TEST_LIBRARY / "filaments" / "photo_stack_models" / "latest.json").write_text(
        json.dumps({"run_id": "published-v2", "path": "published-v2", "model_family": "photo_stack", "model_version": "v2"}),
        encoding="utf-8",
    )

    project_root = _PRISMA_DIR.parent
    transform_source = project_root / "DevelopmentSandbox" / "model_domain_conversion"
    transform_params = json.loads((transform_source / "transform_params.json").read_text(encoding="utf-8"))["v2"]
    camera_generation = "published-v2"
    (_TEST_LIBRARY / "camera_transform").mkdir()
    (_TEST_LIBRARY / "camera_transform" / "CURRENT").write_text(camera_generation + "\n", encoding="utf-8")
    camera_dir = _TEST_LIBRARY / "camera_transform" / camera_generation
    camera_dir.mkdir()
    camera_payload = {
        "schema": "camera_transform_v1",
        "model_version": "v2",
        "n_params": 48,
        "n_knots": 10,
        "used_lattice": False,
        "params": transform_params["params"],
    }
    camera_json = camera_dir / "camera_transform.json"
    camera_lut = camera_dir / "inverse_lut_33.npz"
    camera_json.write_text(json.dumps(camera_payload), encoding="utf-8")
    shutil.copyfile(transform_source / "inverse_lut_33.npz", camera_lut)
    (camera_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_hashes": {
                    "camera_transform.json": hashlib.sha256(camera_json.read_bytes()).hexdigest(),
                    "inverse_lut_33.npz": hashlib.sha256(camera_lut.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )


_build_published_test_library()
PROFILES_DIR = _TEST_LIBRARY / "filaments" / "profiles"
os.environ.setdefault("PRISMA_MODEL_LIBRARY_ROOT", str(_TEST_LIBRARY))
os.environ.setdefault("PRISMA_MODEL_LIBRARIES_ROOT", str(_TEST_RUNTIME_ROOT / "Model Libraries"))
os.environ.setdefault("PRISMA_USER_DATA_ROOT", str(_TEST_RUNTIME_ROOT / "Workspace"))
os.environ.setdefault("PRISMA_IMAGE_ROOT", str(_TEST_RUNTIME_ROOT / "Images"))
os.environ.setdefault("PRISMA_EXPORT_ROOT", str(_TEST_RUNTIME_ROOT / "Exports"))


@pytest.fixture(autouse=True)
def _supply_default_photo_stack_bundle(monkeypatch):
    """Give solve tests a resolvable photo-stack bundle.

    The default appearance provider is ``photo_stack_bundle``, which requires an
    explicit bundle path — in production the server resolves the latest live
    calibration candidate at solve time (``_resolve_photo_stack_candidate_path_for_solve``).
    Tests build raw ``SolveConfig``/``PipelineConfig`` objects and never go through
    that server-layer resolution, so fall back to the tracked reference bundle when
    no path is supplied. Tests that set their own ``photo_stack_bundle_path`` are
    unaffected.
    """
    monkeypatch.setenv("PRISMA_PUBLISHED_LIBRARY_MODE", "1")
    try:
        import appearance_model as am
        from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
    except Exception:
        return  # generator/lib not importable in this context; nothing to patch

    orig_init = am.PhotoStackBundleAppearanceProvider.__init__

    def _init(self, *, bundle_path=None, use_corrections=False):
        if bundle_path is None:
            bundle_path = DEFAULT_PHOTO_STACK_BUNDLE_PATH
            use_corrections = False
        orig_init(self, bundle_path=bundle_path, use_corrections=use_corrections)

    monkeypatch.setattr(am.PhotoStackBundleAppearanceProvider, "__init__", _init)
