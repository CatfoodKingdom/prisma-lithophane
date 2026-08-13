from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import data_paths
import server


_SESSION_TEMPLATE = copy.deepcopy(server.session)


class _ImmediateThread:
    def __init__(self, target, daemon=True):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _write_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")


def _make_fake_result(palette: list[str]) -> SimpleNamespace:
    h, w = 4, 4
    thickness_map = np.full((h, w), 0.20, dtype=np.float32)
    cap_map = np.full((h, w), 0.08, dtype=np.float32)
    de_map = np.full((h, w), 0.01, dtype=np.float32)
    gamut_mask = np.zeros((h, w), dtype=np.uint8)
    stats = SimpleNamespace(
        mean_de=0.01,
        max_de=0.02,
        n_out_of_gamut=0,
        total_pixels=h * w,
        coverage_pct=100.0,
        image_w=w,
        image_h=h,
        max_height=0.28,
        per_filament=[
            SimpleNamespace(
                filament_id=palette[0],
                active_pixels=h * w,
                mean_thickness=0.20,
                max_thickness=0.20,
            )
        ],
    )
    return SimpleNamespace(
        thickness_maps={palette[0]: thickness_map},
        color_profiles={},
        wb_profile={},
        wc_profile={},
        image_domain_width_mm=float(w),
        image_domain_height_mm=float(h),
        solved_plan=None,
        de_map=de_map,
        cap_map=cap_map,
        boundary_cap=None,
        detail_cap=None,
        gamut_mask=gamut_mask,
        debug_maps={},
        cap_quality={},
        stats=stats,
        predict_image=lambda: np.zeros((h, w, 3), dtype=np.uint8),
        predict_image_color_only=lambda: np.zeros((h, w, 3), dtype=np.uint8),
    )


@pytest.fixture
def solve_env(tmp_path, monkeypatch):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    run_cache_dir = tmp_path / "run_cache"
    run_cache_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")

    image_path = images_dir / "solve-input.png"
    Image.new("RGB", (4, 4), "white").save(image_path)

    monkeypatch.setattr(server, "_IMAGES_DIR", images_dir)
    monkeypatch.setattr(server, "_OUTPUT_DIR", output_dir)
    # _current_out_dir resolves the run cache via data_paths at call time, so
    # redirect it to a tmp dir to keep solve artifacts out of the real cache.
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", run_cache_dir)
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server, "load_image", lambda *args, **kwargs: np.zeros((4, 4, 3), dtype=np.uint8))
    monkeypatch.setattr(server, "apply_adjustments", lambda img, *_args, **_kwargs: img)
    monkeypatch.setattr(server, "_save_de_map", lambda _arr, path: _write_placeholder(path))
    monkeypatch.setattr(server, "_save_de_map_scaled", lambda _arr, path, **kwargs: _write_placeholder(path))
    monkeypatch.setattr(server, "_save_cap_height_map", lambda _arr, path, **kwargs: _write_placeholder(path))
    monkeypatch.setattr(server, "_save_overlay_map", lambda _arr, path: _write_placeholder(path))
    monkeypatch.setattr(server, "_save_de_raw", lambda _arr, path, **kwargs: _write_placeholder(path))
    monkeypatch.setattr(server, "_save_surface_blob", lambda _arr, path: _write_placeholder(path))

    captured = {}

    def fake_solve_full(img, sc, progress=None, modules_path=None):
        captured["image_shape"] = img.shape
        captured["solve_config"] = sc
        captured["modules_path"] = modules_path
        return _make_fake_result(sc.palette)

    monkeypatch.setattr(server, "solve_full", fake_solve_full)
    server.session = copy.deepcopy(_SESSION_TEMPLATE)
    server.session["config"].update({
        "image_path": image_path.name,
        "palette": ["demo-filament"],
    })

    return SimpleNamespace(
        client=TestClient(server.app),
        output_dir=output_dir,
        run_cache_dir=run_cache_dir,
        captured=captured,
    )


def test_solve_start_diagnostics_include_active_modules_and_key_settings(solve_env):
    client = solve_env.client
    resp = client.post("/api/session/config", json={
        "solve_pitch_extrusion_width_multiplier": 2,
        "color_region_target_mm": 1.40,
    })
    assert resp.status_code == 200

    start_resp = client.post("/api/solve/start", json={"card_id": "diag_run"})
    assert start_resp.status_code == 200

    diagnostics = server.session["solve"]["result"]["solve_start_diagnostics"]
    assert server.session["solve"]["result"]["diagnostic_palette_version"] == "inferno-v1"
    assert "solver" not in diagnostics["active_modules"]
    assert "grouping" not in diagnostics["active_modules"]
    assert "solver" not in diagnostics["resolved_settings"]
    assert "grouping" not in diagnostics["resolved_settings"]
    assert diagnostics["resolved_settings"]["color_region_target_mm"] == 1.40
    assert diagnostics["resolved_settings"]["image_sample_pitch_mm"] == 0.4
    assert diagnostics["resolved_settings"]["solver_fine_pitch_mm"] == 0.4
    assert diagnostics["resolved_settings"][
        "printability_extrusion_width_mm"
    ] == 0.2
    assert diagnostics["resolved_settings"][
        "printability_minimum_line_length_mm"
    ] == 0.4

    run_json = json.loads(
        (solve_env.run_cache_dir / "diag_run" / "run.json").read_text(encoding="utf-8")
    )
    assert "solver" not in run_json["solve_start_diagnostics"]["active_modules"]
    assert "grouping" not in run_json["solve_start_diagnostics"]["active_modules"]
    assert run_json["diagnostic_palette_version"] == "inferno-v1"
    assert run_json["config"]["printability_extrusion_width_mm"] == 0.2
    assert run_json["config"]["printability_minimum_line_length_mm"] == 0.4


def test_staged_backend_diagnostics_report_only_live_module_slots(solve_env):
    client = solve_env.client
    resp = client.post("/api/session/config", json={
        "stage1_coarsening_factor": 1,
    })
    assert resp.status_code == 200

    start_resp = client.post("/api/solve/start", json={"card_id": "staged_diag_run"})
    assert start_resp.status_code == 200

    diagnostics = server.session["solve"]["result"]["solve_start_diagnostics"]
    assert "solver" not in diagnostics["active_modules"]
    assert "refinements" not in diagnostics["active_modules"]


def test_solve_start_uses_latest_session_config_values(solve_env):
    client = solve_env.client
    first = client.post("/api/session/config", json={
        "color_region_target_mm": 0.60,
        "solve_pitch_extrusion_width_multiplier": 1,
    })
    assert first.status_code == 200

    second = client.post("/api/session/config", json={
        "color_region_target_mm": 1.80,
        "solve_pitch_extrusion_width_multiplier": 2,
    })
    assert second.status_code == 200

    start_resp = client.post("/api/solve/start", json={"card_id": "latest_cfg"})
    assert start_resp.status_code == 200

    solve_config = solve_env.captured["solve_config"]
    assert solve_config.color_region_target_mm == pytest.approx(1.80)
    assert solve_config.image_sample_pitch_mm == pytest.approx(0.40)
    assert solve_config.solver_fine_pitch_mm == pytest.approx(0.40)
    assert Path(solve_env.captured["modules_path"]) == solve_env.output_dir.parent / "modules.json"


def test_solve_start_carries_preprocessing_params_into_solve_config_and_run_json(solve_env):
    client = solve_env.client
    params = {
        "b1_printscale_bilateral": {
            "feature_scale_multiplier": 0.8,
            "sigma_range": 0.035,
            "passes": 1,
        }
    }

    resp = client.post("/api/session/config", json={
        "preprocessing_params": params,
    })
    assert resp.status_code == 200

    start_resp = client.post("/api/solve/start", json={"card_id": "pre_params"})
    assert start_resp.status_code == 200

    assert solve_env.captured["solve_config"].preprocessing_params == params

    run_json = json.loads(
        (solve_env.run_cache_dir / "pre_params" / "run.json").read_text(encoding="utf-8")
    )
    assert run_json["config"]["preprocessing_params"] == params


def test_solve_status_and_session_share_terminal_progress_contract(solve_env):
    client = solve_env.client
    started = client.post("/api/solve/start", json={"card_id": "progress_contract"})
    assert started.status_code == 200
    job_id = started.json()["job_id"]

    status = client.get("/api/solve/status").json()
    restored = client.get("/api/session").json()["solve"]
    assert status == restored
    assert status["job_id"] == job_id
    assert status["card_id"] == "progress_contract"
    assert status["status"] == "complete"
    assert status["progress"] == "Solve complete"
    assert status["progress_detail"]["overall_pct"] == 100.0
    assert status["progress_detail"]["stage_index"] == 7
    assert status["progress_detail"]["stage_count"] == 7
