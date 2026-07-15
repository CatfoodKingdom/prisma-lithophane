"""Contract tests for photo-stack model/appearance provider aliases."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
_PRISMA_DIR = _ROOT / "Prisma"
for _p in (_GEN_DIR, _PRISMA_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from appearance_model import PhotoStackBundleAppearanceProvider, StackRequest
from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
from lib.photo_stack_model.predictor import MODEL_NAME as PHOTO_STACK_MODEL_NAME

WHITE = "bambu-tough-white"
BLUE = "chrominal-deep-sea-blue"
TRANS_CYAN = "panchroma-translucent-cyan"
YELLOW = "bambu-basic-yellow"


def _write_candidate(tmp_path: Path) -> Path:
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    shutil.copy2(DEFAULT_PHOTO_STACK_BUNDLE_PATH, run_dir / "runtime_bundle.json")
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "candidate"}) + "\n", encoding="utf-8")
    (run_dir / "model.json").write_text(
        json.dumps({"runtime_bundle_path": "runtime_bundle.json"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "correction_layer.json").write_text(
        json.dumps(
            {
                "schema": "prisma_photo_stack_v2_correction",
                "schema_version": 1,
                "correction_layer_version": "identity",
                "base_model_name": PHOTO_STACK_MODEL_NAME,
                "training_rows": [],
                "training_row_count": 0,
                "parameters": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_photo_stack_appearance_methods_are_exact_model_pass_throughs(tmp_path: Path) -> None:
    provider = PhotoStackBundleAppearanceProvider(
        bundle_path=_write_candidate(tmp_path),
        use_corrections=True,
    )
    requests = [
        StackRequest((WHITE, 0.2), (), (WHITE, 0.4)),
        StackRequest((WHITE, 0.2), ((BLUE, 0.2),), (WHITE, 0.4)),
        StackRequest((WHITE, 0.2), ((TRANS_CYAN, 0.2), (YELLOW, 0.2)), (WHITE, 0.32)),
    ]

    model_batch = provider.predict_stack_model_linear_rgb_batch(requests)
    appearance_batch = provider.predict_stack_appearance_linear_rgb_batch(requests)

    np.testing.assert_array_equal(appearance_batch, model_batch)

    maps = {
        BLUE: np.asarray([[0.2, 0.4], [0.0, 0.2]], dtype=np.float32),
        TRANS_CYAN: np.asarray([[0.0, 0.2], [0.0, 0.0]], dtype=np.float32),
        "__white_cap__": np.asarray([[0.2, 0.4], [0.2, 0.2]], dtype=np.float32),
    }
    model_map = provider.predict_thickness_maps_model_linear_rgb(
        thickness_maps=maps,
        white_base=(WHITE, 0.2),
        white_cap_id=WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[BLUE, TRANS_CYAN],
    )
    appearance_map = provider.predict_thickness_maps_appearance_linear_rgb(
        thickness_maps=maps,
        white_base=(WHITE, 0.2),
        white_cap_id=WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[BLUE, TRANS_CYAN],
    )

    np.testing.assert_array_equal(appearance_map, model_map)
