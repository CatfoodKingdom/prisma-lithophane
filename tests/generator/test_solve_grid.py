from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import server
from solve_grid import SolveGridResolutionError, resolve_solve_grid


_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "solve_grid_rounding_cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(autouse=True)
def _restore_session_config():
    original = copy.deepcopy(server.session["config"])
    try:
        yield
    finally:
        server.session["config"] = original


@pytest.mark.parametrize("case", _VECTORS, ids=lambda case: case["name"])
def test_canonical_grid_matches_shared_rounding_vectors(case):
    result = resolve_solve_grid(
        case["width_mm"],
        case["height_mm"],
        case["pitch_mm"],
    )

    assert result["rounding_mode"] == "half_up"
    assert result["cells"] == case["cells"]
    assert result["resolved"] == case["resolved"]
    assert result["aligned"]["all"] is case["aligned"]


def test_pitch_too_large_for_supported_range_is_invalid():
    with pytest.raises(SolveGridResolutionError, match="too large"):
        resolve_solve_grid(100.0, 100.0, 301.0)


def test_session_config_returns_authoritative_resolved_geometry():
    response = TestClient(server.app).post(
        "/api/session/config",
        json={
            "frame": {"width_mm": 100.2, "height_mm": 100.2},
            "solve_pitch_extrusion_width_multiplier": 2,
        },
    )

    assert response.status_code == 200, response.text
    grid = response.json()["resolved_solve_grid"]
    assert grid["requested"] == {"width_mm": 100.2, "height_mm": 100.2}
    assert grid["cells"] == {"width": 251, "height": 251}
    assert grid["resolved"] == {"width_mm": 100.4, "height_mm": 100.4}
    assert grid["aligned"] == {"width": False, "height": False, "all": False}


def test_session_config_repairs_multiplier_without_an_in_range_grid():
    response = TestClient(server.app).post(
        "/api/session/config",
        json={
            "frame": {"width_mm": 100.0, "height_mm": 100.0},
            "solve_pitch_extrusion_width_multiplier": 2000,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["print_setup_repair"]["code"] == "solve_pitch_multiplier_clamped"
    assert body["config"]["solve_pitch_extrusion_width_multiplier"] < 2000


def test_production_image_load_uses_explicit_grid_and_upscales_small_sources(tmp_path):
    source = tmp_path / "tiny.png"
    Image.fromarray(np.zeros((3, 2, 3), dtype=np.uint8), mode="RGB").save(source)
    config = {
        "frame": {"width_mm": 100.2, "height_mm": 80.0},
        "image_sample_pitch_mm": 0.4,
        "solver_fine_pitch_mm": 0.4,
        "max_dim_mm": 130.0,
        "source_resample_kernel": "lanczos",
        "image_adjust": None,
    }

    loaded = server._load_run_source_image(
        source,
        config,
        resolved_source=SimpleNamespace(working_path=source),
    )

    assert loaded.shape == (200, 251, 3)


def test_explicit_coarse_image_load_preserves_capped_longest_edge_behavior(tmp_path):
    source = tmp_path / "large.png"
    Image.fromarray(np.zeros((500, 1000, 3), dtype=np.uint8), mode="RGB").save(source)
    config = {
        "frame": {"width_mm": 100.2, "height_mm": 80.0},
        "image_sample_pitch_mm": 0.4,
        "solver_fine_pitch_mm": 0.4,
        "max_dim_mm": 130.0,
        "source_resample_kernel": "lanczos",
        "image_adjust": None,
    }

    loaded = server._load_run_source_image(
        source,
        config,
        max_dim_mm=80.0,
        resolved_source=SimpleNamespace(working_path=source),
    )

    assert loaded.shape == (160, 200, 3)
