"""Canonical-only ingress tests for live session config and profiles."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture(autouse=True)
def _reset_session_config():
    original = copy.deepcopy(server.session["config"])
    try:
        yield
    finally:
        server.session["config"] = original


@pytest.fixture
def client():
    return TestClient(server.app)


def _assert_canonical_resolution_only(cfg: dict) -> None:
    assert cfg["image_sample_pitch_mm"] == pytest.approx(0.25)
    assert cfg["solver_fine_pitch_mm"] == pytest.approx(0.25)
    assert cfg["color_region_target_mm"] == pytest.approx(0.80)
    assert "pixel_size_mm" not in cfg
    assert "color_pixel_mm" not in cfg


def test_session_config_accepts_canonical_resolution_and_egress_is_canonical_only(client):
    resp = client.post("/api/session/config", json={
        "image_sample_pitch_mm": 0.25,
        "solver_fine_pitch_mm": 0.25,
        "color_region_target_mm": 0.80,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_canonical_resolution_only(body["config"])

    readback = client.get("/api/session/config")
    assert readback.status_code == 200, readback.text
    _assert_canonical_resolution_only(readback.json())


def test_session_config_rejects_legacy_pixel_size_alias(client):
    resp = client.post("/api/session/config", json={"pixel_size_mm": 0.25})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "legacy_resolution_field"
    assert "image_sample_pitch_mm" in detail["message"]
    assert "solver_fine_pitch_mm" in detail["message"]


def test_session_config_rejects_legacy_color_alias(client):
    resp = client.post("/api/session/config", json={"color_pixel_mm": 0.80})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "legacy_resolution_field"
    assert "color_region_target_mm" in detail["message"]


def test_session_config_rejects_legacy_mesh_xy_pitch_alias(client):
    resp = client.post("/api/session/config", json={"mesh_xy_pitch_mm": 0.25})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "legacy_resolution_field"
    assert detail["field"] == "mesh_xy_pitch_mm"
    assert "image_sample_pitch_mm" in detail["message"]
    assert "solver_fine_pitch_mm" in detail["message"]


@pytest.mark.parametrize("field", [
    "smooth_stl",
    "solver_backend",
    # Task 2.1a retired cap-shaping fields (top-level tv_weight is the retired
    # tv_denoise field, NOT the live nested preprocessing_params B3 tv_weight).
    "guided_surface_mode",
    "guided_surface_radius_mm",
    "guided_surface_eps",
    "guided_surface_gaussian_sigma_mm",
    "hybrid_relax_strength",
    "hybrid_relax_radius_mm",
    "hybrid_edge_guard",
    "hybrid_underfill_bias",
    "tv_weight",
    # Task 2.1b dead cap convergence/significance fields.
    "cap_convergence_mm",
    "cap_significant_layers",
    # Task 2.2a orphaned raster-cleanup params (consumer MinFeatureCleanupRefinement
    # deleted with refinements/ on 2026-06-12).
    "cleanup_reassign_mode",
    "cleanup_search_radius_mm",
    # Task 2.2b: retired the last two cleanup_* fields after re-anchoring Wing-B
    # preprocessing to nozzle_diameter (2 x nozzle). cleanup_min_width_mm was the
    # feature-scale anchor; cleanup_min_area_mm2 fed (dormant) blueprint-triage.
    "cleanup_min_width_mm",
    "cleanup_min_area_mm2",
    # Task 2.3: retired the translucent-underfill config family with the feature.
    "v2_translucent_underfill_enabled",
    "v2_translucent_underfill_filament",
    "v2_translucent_underfill_max_mm",
    "v2_translucent_underfill_de_budget",
    "v2_translucent_underfill_white_skin_mm",
    "v2_translucent_preferred_visible_skin_mode",
    "v2_translucent_support_target_mode",
    "v2_translucent_underfill_policy",
    "v2_translucent_underfill_safe_subset_erosion_px",
    "v2_translucent_underfill_safe_subset_min_debt_layers",
    "v2_translucent_underfill_chooser_de_weight",
    "v2_translucent_underfill_component_activation_cost",
    "printability_preferred_line_length_mm",
])
def test_session_config_rejects_retired_config_fields(client, field):
    resp = client.post("/api/session/config", json={field: "stale"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "retired_config_field"
    assert detail["field"] == field


def test_session_config_rejects_retired_preferred_line_length_even_when_null(client):
    resp = client.post(
        "/api/session/config",
        json={"printability_preferred_line_length_mm": None},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "retired_config_field"
    assert detail["field"] == "printability_preferred_line_length_mm"


def test_session_config_ignores_unknown_non_retired_extras(client):
    resp = client.post("/api/session/config", json={
        "color_region_target_mm": 0.95,
        "unknown_scratch_field": "ignored",
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["color_region_target_mm"] == pytest.approx(0.95)
    assert "unknown_scratch_field" not in cfg


def test_session_config_quiet_drops_legacy_run_logging(client):
    r = client.post("/api/session/config", json={"run_logging": True})
    assert r.status_code == 200
    assert "run_logging" not in r.json()["config"]


def test_session_config_quiet_drops_retired_preview_resolution(client):
    resp = client.post("/api/session/config", json={"preview_resolution": 0.5})

    assert resp.status_code == 200, resp.text
    assert "preview_resolution" not in resp.json()["config"]


def test_session_config_allows_repeated_canonical_updates(client):
    first = client.post("/api/session/config", json={
        "image_sample_pitch_mm": 0.25,
        "solver_fine_pitch_mm": 0.25,
        "color_region_target_mm": 0.60,
    })
    assert first.status_code == 200, first.text

    second = client.post("/api/session/config", json={
        "color_region_target_mm": 1.00,
    })
    assert second.status_code == 200, second.text
    assert second.json()["config"]["color_region_target_mm"] == pytest.approx(1.00)


def test_session_config_accepts_staged_backend_controls(client):
    resp = client.post("/api/session/config", json={
        "stage1_coarsening_factor": 2,
        "emit_blueprint_printability": True,
        "printability_minimum_extrusion_width_mm": 0.2,
        "printability_minimum_line_length_mm": 0.5,
        "stage2_boundary_mutation_enabled": True,
        "stage2_boundary_mutation_current_de_percentile": 80.0,
        "stage2_boundary_mutation_max_passes": 12,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["stage1_coarsening_factor"] == 2
    assert cfg["emit_blueprint_printability"] is True
    assert cfg["printability_minimum_extrusion_width_mm"] == pytest.approx(0.2)
    assert cfg["printability_minimum_line_length_mm"] == pytest.approx(0.5)
    assert cfg["stage2_boundary_mutation_enabled"] is True
    assert "stage2_boundary_mutation_edge_run_mode" not in cfg
    assert cfg["stage2_boundary_mutation_current_de_percentile"] == pytest.approx(80.0)
    assert cfg["stage2_boundary_mutation_max_passes"] == 12


def test_session_config_drops_retired_boundary_mutation_switches(client):
    resp = client.post("/api/session/config", json={
        "stage2_boundary_mutation_enabled": True,
        "stage2_boundary_mutation_segment_mode": True,
        "stage2_boundary_mutation_edge_run_mode": True,
    })

    assert resp.status_code == 200, resp.text
    cfg = client.get("/api/session/config").json()
    assert cfg["stage2_boundary_mutation_enabled"] is True
    assert "stage2_boundary_mutation_segment_mode" not in cfg
    assert "stage2_boundary_mutation_edge_run_mode" not in cfg


def test_palette_suggest_payload_accepts_luminance_mode():
    payload = server.PaletteSuggestPayload(
        image_path="example.jpg",
        palette_mode="luminance-detail",
    )

    assert payload.palette_mode == "luminance_detail"


def test_palette_suggest_payload_rejects_unknown_mode():
    with pytest.raises(ValueError):
        server.PaletteSuggestPayload(
            image_path="example.jpg",
            palette_mode="not-a-mode",
        )


def test_luminance_base_shading_limit_recommendation_is_bounded_and_metric_backed():
    image = np.zeros((12, 12, 3), dtype=np.uint8)
    image[:, :6] = np.array([245, 245, 245], dtype=np.uint8)
    image[:, 6:] = np.array([25, 25, 25], dtype=np.uint8)

    recommendation = server._recommend_luminance_base_shading_limit_fraction(image)

    assert 0.60 <= recommendation["recommended_base_shading_limit_fraction"] <= 0.90
    assert 0.60 <= recommendation["recommended_authority_fraction"] <= 0.90
    assert "luminance_range_p95_p05" in recommendation["metrics"]


def test_session_config_accepts_base_shading_limit_alias(client):
    resp = client.post("/api/session/config", json={
        "luminance_base_shading_limit_fraction": 0.42,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["luminance_base_shading_limit_fraction"] == pytest.approx(0.42)
    assert cfg["luminance_handler_optical_authority_fraction"] == pytest.approx(0.42)

    readback = client.get("/api/session/config")
    assert readback.status_code == 200, readback.text
    assert readback.json()["luminance_base_shading_limit_fraction"] == pytest.approx(0.42)


def test_luminance_mode_preset_preserves_base_shading_limit_alias(client):
    resp = client.post("/api/session/config", json={
        "luminance_mode": "luminance_detail",
        "luminance_base_shading_limit_fraction": 0.63,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["luminance_mode"] == "luminance_detail"
    assert cfg["luminance_base_shading_limit_fraction"] == pytest.approx(0.63)
    assert cfg["luminance_handler_optical_authority_fraction"] == pytest.approx(0.63)
    assert cfg["luminance_handler_enabled"] is True
    assert cfg["luminance_detail_authoring_printability"] == "absolute_finalgate"


def test_create_settings_profile_uses_canonical_only_settings(client):
    resp = client.post("/api/settings-profiles", json={
        "name": "canonical-profile",
        "settings": {
            "image_sample_pitch_mm": 0.25,
            "solver_fine_pitch_mm": 0.25,
            "color_region_target_mm": 0.80,
        },
        "modules": {},
    })
    assert resp.status_code == 200, resp.text
    profiles = resp.json()["profiles"]
    created = next(p for p in profiles if p["name"] == "canonical-profile")
    settings = created["settings"]
    _assert_canonical_resolution_only(settings)
    client.delete(f"/api/settings-profiles/{created['id']}")


def test_create_settings_profile_rejects_legacy_aliases(client):
    resp = client.post("/api/settings-profiles", json={
        "name": "legacy-profile",
        "settings": {"pixel_size_mm": 0.25},
        "modules": {},
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "legacy_resolution_field"
    assert "image_sample_pitch_mm" in detail["message"]


def test_update_settings_profile_rejects_legacy_aliases(client):
    create = client.post("/api/settings-profiles", json={
        "name": "to-update",
        "settings": {
            "image_sample_pitch_mm": 0.25,
            "solver_fine_pitch_mm": 0.25,
            "color_region_target_mm": 0.80,
        },
        "modules": {},
    })
    profile_id = next(
        p["id"] for p in create.json()["profiles"] if p["name"] == "to-update"
    )
    try:
        resp = client.put(f"/api/settings-profiles/{profile_id}", json={
            "name": "to-update",
            "settings": {"color_pixel_mm": 0.80},
            "modules": {},
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "legacy_resolution_field"
    finally:
        client.delete(f"/api/settings-profiles/{profile_id}")


def test_settings_profiles_api_surfaces_nested_profiles_and_keeps_top_level_crud_working(
    client, tmp_path, monkeypatch
):
    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    wing_c_dir = settings_dir / "wing-c"
    wing_c_dir.mkdir(parents=True, exist_ok=True)
    (wing_c_dir / "minimal.json").write_text(json.dumps({
        "id": "wing-c-minimal",
        "kind": "named",
        "name": "Wing C Minimal",
        "settings": {},
        "modules": {},
    }), encoding="utf-8")
    (wing_c_dir / "standard.json").write_text(json.dumps({
        "id": "wing-c-standard",
        "kind": "named",
        "name": "Wing C Standard",
        "settings": {},
        "modules": {},
    }), encoding="utf-8")

    initial = client.get("/api/settings-profiles")
    assert initial.status_code == 200, initial.text
    initial_ids = {profile["id"] for profile in initial.json()["profiles"]}
    assert server._SYSTEM_SETTINGS_PROFILE_ID in initial_ids
    assert "wing-c-minimal" in initial_ids
    assert "wing-c-standard" in initial_ids

    created = client.post("/api/settings-profiles", json={
        "name": "top-level-profile",
        "settings": {},
        "modules": {},
    })
    assert created.status_code == 200, created.text
    created_profile = next(
        profile
        for profile in created.json()["profiles"]
        if profile["name"] == "top-level-profile"
    )

    updated = client.put(f"/api/settings-profiles/{created_profile['id']}", json={
        "name": "top-level-renamed",
        "settings": {},
        "modules": {},
    })
    assert updated.status_code == 200, updated.text
    updated_profile = next(
        profile
        for profile in updated.json()["profiles"]
        if profile["id"] == created_profile["id"]
    )
    assert updated_profile["name"] == "top-level-renamed"
    assert "grouping_mode" not in updated_profile["settings"]

    deleted = client.delete(f"/api/settings-profiles/{created_profile['id']}")
    assert deleted.status_code == 200, deleted.text
    deleted_ids = {profile["id"] for profile in deleted.json()["profiles"]}
    assert created_profile["id"] not in deleted_ids
    assert "wing-c-minimal" in deleted_ids
    assert "wing-c-standard" in deleted_ids


def test_run_json_contains_canonical_resolution_block(tmp_path):
    run_dir = tmp_path / "run-abc"
    run_dir.mkdir()
    server._write_run_json(run_dir, {
        "timestamp": "run-abc",
        "image": "",
        "palette": ["bambu-basic-white"],
        "profile_ref": None,
        "profile_name_at_solve": None,
        "is_profile_modified_at_solve": False,
        "recipe_snapshot": None,
        "config": {
            "image_sample_pitch_mm": 0.25,
            "solver_fine_pitch_mm": 0.25,
            "color_region_target_mm": 0.80,
            "pixel_size_mm": 0.25,
            "color_pixel_mm": 0.80,
        },
        "stats": {},
    })

    data = json.loads((run_dir / "run.json").read_text())
    assert "pixel_size_mm" not in data["config"]
    assert "color_pixel_mm" not in data["config"]
    assert data["resolved_resolution"]["image_sample_pitch_mm"] == pytest.approx(0.25)
    assert data["resolved_resolution"]["solver_fine_pitch_mm"] == pytest.approx(0.25)
    assert data["resolved_resolution"]["color_region_target_mm"] == pytest.approx(0.80)
