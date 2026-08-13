"""Round-trip oracle for the Save/Load run archive (Stage 9b, Slice 3, Task 3.2).

ORACLE LEVEL IMPLEMENTED: **fallback** (NOT the full 3MF mesh compare).

Why the fallback: the plan's preferred form runs a REAL mesh export
(``_materialize_post_solve_export_bundle_from_cached_solve`` ->
``export_solve_bundle`` -> ``write_export_mesh_bundle_as_3mf``) in-test and
compares the deterministic ``3D/3dmodel.model`` member. That path needs a
genuinely export-valid solve: a full geometry-bearing config plus real field
reconstruction over the thickness field. With the lightweight synthetic
fixture this module builds (a 4x4 fake solve, no real solver run), the heavy
mesh export is both slow and fragile and would not be a faithful geometry
oracle. The plan explicitly sanctions the documented fallback in exactly this
situation.

What the fallback genuinely exercises (per the plan):
  (a) solve (synthetic) -> save -> clear cache -> load yields a COMPLETE cached
      entry under a FRESH ``loaded-*`` card_id (never clobbering the original); and
  (b) the export-owned swap builder SUCCEEDS on the loaded card. Swap instruction
      generation reads the reserved white-cap map
      (``MapKey.WHITE_CAP``), the per-filament color maps, ``d_wb`` /
      ``layer_height`` from the rehydrated config, AND requires non-None
      ``image_domain_width_mm`` / ``image_domain_height_mm`` (it raises HTTP 500
      otherwise). So a passing swap call proves those geometry-bearing fields
      survived the pack -> archive -> load round trip — which is the property the
      full 3MF compare would also have proven.

The cached solve the fixture produces therefore MUST contain the reserved
white-cap map under ``MapKey.WHITE_CAP`` and non-None image_domain dims, or both
export and swap would raise and the oracle would test nothing.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import auto_run_store
import data_paths
import run_archive
import run_store
import server
from source_images import SourceImageService
from thickness_maps import MapKey
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)

_SESSION_TEMPLATE = copy.deepcopy(server.session)


def _export_contract_for_shape(shape: tuple[int, int]) -> tuple[dict, dict]:
    field = np.full(shape, 0.36, np.float32)
    metadata = {
        PHYSICAL_GEOMETRY_METADATA_KEY: {
            "pitch_mm": 0.20,
            "solver_fine_pitch_mm": 0.20,
            "layer_height_mm": 0.08,
            "d_wb_mm": 0.20,
            "d_wc_min_mm": 0.08,
            "t_max_mm": 4.0,
            "luminance_mode": "standard",
            "cap_mode": "smooth_variable",
        },
        WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
            "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
            "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
            "policy": "standard_smooth_variable_canonical",
            "solve_mode": "standard",
            "luminance_mode": "standard",
            "cap_mode": "smooth_variable",
            "detail_smoothing_applied": False,
            "effective_d_wc_max_mm": 0.16,
            "effective_boundary_d_wc_max_mm": 0.16,
            "required_cover_floor_mm": 0.08,
        },
    }
    return {WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: field}, metadata


def _seed_export_valid_solve(card_id: str) -> None:
    """Seed a cached solve that is valid for BOTH /api/export/files and swap.

    The reserved white-cap map MUST be keyed by the exact MapKey string and the
    image_domain dims MUST be non-None, or export/swap raise.
    """
    run_dir = data_paths.RUN_CACHE_DIR / card_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "predicted.png").write_bytes(b"png-bytes")
    cfg = {
        "image_path": "steve.jpg",
        "palette": ["demo-filament"],
        "d_wb": 0.20,
        "layer_height": 0.08,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "ams_slots": 4,
        "white_slots": 1,
        "white_base": "bambu-tough-white",
        "white_cap": None,
    }
    export_maps, export_metadata = _export_contract_for_shape((4, 4))
    server.session["solve_cache"][card_id] = {
        "config": cfg,
        "solve": {
            "status": "complete",
            "card_id": card_id,
            # Reserved key MUST be the exact MapKey string (MapKey subclasses str);
            # one active color filament so reconcile/plan groups has something to place.
            "thickness_maps": {
                MapKey.WHITE_CAP: np.full((4, 4), 0.16, np.float32),
                MapKey.WHITE_BOUNDARY_CAP: np.full((4, 4), 0.08, np.float32),
                MapKey.WHITE_DETAIL_CAP: np.full((4, 4), 0.08, np.float32),
                "demo-filament": np.full((4, 4), 0.20, np.float32),
            },
            "debug_maps": {"de_map": np.full((4, 4), 0.01, np.float32)},
            "export_maps": export_maps,
            "export_metadata": export_metadata,
            "image_domain_width_mm": 100.0,
            "image_domain_height_mm": 80.0,
            "grouping": None,
            "solved_plan": None,
            "solve_owned_fingerprint": "fp-original",
            "result": {
                "mean_de": 0.01,
                "max_de": 0.02,
                "card_id": card_id,
                "predicted_url": f"/api/run-cache/files/predicted.png?run={card_id}&t=1",
            },
        },
    }


def _seed_banded_export_valid_solve(card_id: str) -> dict:
    """Seed a two-band solve whose persisted grouping is required after load."""
    _seed_export_valid_solve(card_id)
    cached = server.session["solve_cache"][card_id]
    cfg = cached["config"]
    solve = cached["solve"]
    palette = ["a", "b", "c", "d"]
    grouping = {
        "groups": [["a", "b"], ["c", "d"]],
        "band_layers": [2, 2],
        "canonical_palette": palette,
        "layer_height_mm": 0.08,
        "d_wb_mm": 0.20,
        "pause_z_mm": [0.36],
        "band_heights_mm": [0.16, 0.16],
        "banding_cost": {"median_de_delta": 0.0},
    }
    shape = (4, 4)
    cfg.update({
        "palette": palette,
        "ams_slots": 3,
        "white_slots": 1,
        "min_cap_layers": 1,
    })
    solve["thickness_maps"] = {
        MapKey.WHITE_CAP: np.full(shape, 0.16, np.float32),
        MapKey.WHITE_BOUNDARY_CAP: np.full(shape, 0.08, np.float32),
        MapKey.WHITE_DETAIL_CAP: np.full(shape, 0.08, np.float32),
        "a": np.full(shape, 0.08, np.float32),
        "b": np.zeros(shape, np.float32),
        "c": np.full(shape, 0.08, np.float32),
        "d": np.zeros(shape, np.float32),
    }
    solve["export_maps"][WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY] = np.full(
        shape,
        0.44,
        np.float32,
    )
    solve["swap_grouping"] = copy.deepcopy(grouping)
    solve["result"]["staged_metrics"] = {
        "swap_grouping": copy.deepcopy(grouping),
        "swap_plan_availability": {
            "available": True,
            "appearance_model": "photo_stack_bundle",
        },
    }
    return grouping


@pytest.fixture
def roundtrip_env(tmp_path, monkeypatch):
    server.session = copy.deepcopy(_SESSION_TEMPLATE)
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "saved")
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", tmp_path / "runs")
    monkeypatch.setattr(data_paths, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "_IMAGES_DIR", tmp_path / "photos")
    for sub in ("saved", "runs", "output", "photos"):
        (tmp_path / sub).mkdir()
    image_buf = io.BytesIO()
    Image.new("RGB", (8, 6), (32, 96, 160)).save(image_buf, format="JPEG")
    (server._IMAGES_DIR / "steve.jpg").write_bytes(image_buf.getvalue())
    monkeypatch.setattr(
        server,
        "_SOURCE_IMAGES",
        SourceImageService(server._IMAGES_DIR, tmp_path / "source-cache"),
    )
    client = TestClient(server.app)
    try:
        yield SimpleNamespace(client=client, server=server, tmp_path=tmp_path)
    finally:
        server.session = copy.deepcopy(_SESSION_TEMPLATE)


def _assert_load_critical_archives_match(left, right):
    assert set(left.thickness_arrays) == set(right.thickness_arrays)
    for key in left.thickness_arrays:
        assert left.thickness_arrays[key].shape == right.thickness_arrays[key].shape
    for key in ("palette", "config", "image_domain_width_mm", "image_domain_height_mm"):
        assert left.run_json[key] == right.run_json[key]
    assert left.run_json["export_metadata"] == right.run_json["export_metadata"]
    assert set(left.run_cache_files) == set(right.run_cache_files)


def _build_export_swap_payload(server_module, card_id: str) -> dict:
    """Exercise the same materialization and swap builder used by export finalization."""
    solve, cfg, _target_card_id = server_module._resolve_export_target(card_id)
    export_thickness_maps, ordering = server_module._prepare_export_materialization(
        cfg,
        solve["thickness_maps"],
    )
    return server_module._build_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=export_thickness_maps,
        ordering=ordering,
    )


def _assert_no_phantom_settings(mapping: dict) -> None:
    assert server._PHANTOM_CONFIG_FIELDS.isdisjoint(mapping)


def _archive_settings_carriers(run_json: dict) -> list[dict]:
    metadata = run_json.get("run_metadata") or {}
    recipe = metadata.get("recipe_snapshot") or {}
    profile = recipe.get("profile_snapshot") or {}
    diagnostics = metadata.get("solve_start_diagnostics") or {}
    result = run_json.get("result") or {}
    compact_diagnostics = result.get("solve_start_diagnostics") or {}
    return [
        run_json.get("config") or {},
        metadata.get("config") or {},
        recipe.get("config") or {},
        profile.get("settings") or {},
        diagnostics.get("resolved_settings") or {},
        compact_diagnostics.get("resolved_settings") or {},
    ]


def _stale_phantom_values() -> dict:
    return {
        "smooth_iters": 19,
        "allow_print_despite_hazards": True,
        "detail_cap_pitch_mm": 0.07,
        "v2_cleanup_de_budget": 0.31,
        "v2_enable_cliff_closure": False,
        "v2_enable_cap_topology_cleanup": True,
        "v2_max_cleanup_rounds": 9,
        "v2_full_cap_quality_report": True,
        "swap_improvement_threshold": 3.5,
        "force_all_tiers": True,
    }


def test_auto_archive_matches_explicit_save_at_pre_export_completion(roundtrip_env, monkeypatch):
    env = roundtrip_env
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", env.tmp_path / "auto")
    data_paths.AUTO_RUNS_DIR.mkdir()
    _seed_export_valid_solve("orig")

    saved_at = "20260616-101500"
    solve = server.session["solve_cache"]["orig"]["solve"]
    cfg = server.session["solve_cache"]["orig"]["config"]

    # Auto path: write through the same helper the solve-completion hook calls.
    server._maybe_write_auto_run("orig", solve, cfg, saved_at=saved_at)
    auto_id = auto_run_store.list_auto_runs()[0]["save_id"]
    auto = run_archive.read_run_archive(auto_run_store.read_auto_zip_bytes(auto_id))

    # Explicit Save at the SAME pre-export moment. This proves auto captured the
    # complete solve-time run-cache subtree, not a subset/different root.
    _explicit_id, explicit_zip, _explicit_sidecar = server._pack_completed_run_archive(
        "orig", solve, cfg, label="explicit", saved_at=saved_at,
        root=data_paths.SAVED_RUNS_DIR, tier="saved")
    explicit = run_archive.read_run_archive(explicit_zip)

    assert "post_solve_export_bundle/arrays.npz" not in auto.run_cache_files
    assert f"ex__{WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY}" in auto.thickness_arrays
    assert WHITE_CAP_FIELD_TARGET_METADATA_KEY in auto.run_json["export_metadata"]
    _assert_load_critical_archives_match(auto, explicit)


def test_loaded_auto_record_reexports_without_prebaked_export_bundle(roundtrip_env, monkeypatch):
    env = roundtrip_env
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", env.tmp_path / "auto")
    data_paths.AUTO_RUNS_DIR.mkdir()
    _seed_export_valid_solve("orig")
    solve = server.session["solve_cache"]["orig"]["solve"]
    cfg = server.session["solve_cache"]["orig"]["config"]

    server._maybe_write_auto_run("orig", solve, cfg, saved_at="20260616-101500")
    auto_id = auto_run_store.list_auto_runs()[0]["save_id"]
    parsed_auto = run_archive.read_run_archive(auto_run_store.read_auto_zip_bytes(auto_id))
    assert all(not rel.startswith("post_solve_export_bundle/") for rel in parsed_auto.run_cache_files)

    server.session["solve_cache"].clear()
    loaded = env.client.post("/api/runs/load", json={"save_id": auto_id, "tier": "auto"}).json()
    loaded_card = loaded["card_id"]

    # This must use the rehydrated card, not the original in-memory solve.
    assert loaded_card != "orig"
    assert "orig" not in server.session["solve_cache"]
    entry = server.session["solve_cache"][loaded_card]["solve"]
    cfg = server.session["solve_cache"][loaded_card]["config"]
    assert WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY in entry["export_maps"]
    assert WHITE_CAP_FIELD_TARGET_METADATA_KEY in entry["export_metadata"]

    # Preferred proof: the export-prep path rebuilds post_solve_export_bundle from
    # cached thickness_maps + cfg + debug_maps; it does not need a bundled copy.
    bundle = server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id=loaded_card,
        solve=entry,
        cfg=cfg,
        thickness_maps=entry["thickness_maps"],
        ordering=list(cfg.get("palette") or []),
    )
    assert (bundle / "arrays.npz").exists()
    assert (bundle / "run_template.json").exists()
    with np.load(bundle / "arrays.npz") as arrays:
        assert WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY in arrays.files
    metadata = json.loads((bundle / "run_template.json").read_text(encoding="utf-8"))
    assert metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY]["field_key"] == (
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    )

    # Fallback proof already used by Stage 9b if export fixture setup is too heavy:
    # swap instructions must still succeed on the loaded auto record.
    assert "gcode" in _build_export_swap_payload(server, loaded_card)


def test_materializer_requires_fresh_canonical_export_contract(roundtrip_env):
    _seed_export_valid_solve("orig")
    entry = server.session["solve_cache"]["orig"]["solve"]
    cfg = server.session["solve_cache"]["orig"]["config"]
    entry["export_maps"] = {}

    with pytest.raises(server.HTTPException) as exc:
        server._materialize_post_solve_export_bundle_from_cached_solve(
            card_id="orig",
            solve=entry,
            cfg=cfg,
            thickness_maps=entry["thickness_maps"],
            ordering=list(cfg.get("palette") or []),
        )

    assert exc.value.status_code == 409
    assert "fresh white-cap canonical fields" in exc.value.detail


@pytest.mark.parametrize("missing_key", [MapKey.WHITE_BOUNDARY_CAP, MapKey.WHITE_DETAIL_CAP])
def test_materializer_requires_fresh_boundary_and_detail_cap_maps(roundtrip_env, missing_key):
    _seed_export_valid_solve("orig")
    entry = server.session["solve_cache"]["orig"]["solve"]
    cfg = server.session["solve_cache"]["orig"]["config"]
    entry["thickness_maps"].pop(missing_key)

    with pytest.raises(server.HTTPException) as exc:
        server._materialize_post_solve_export_bundle_from_cached_solve(
            card_id="orig",
            solve=entry,
            cfg=cfg,
            thickness_maps=entry["thickness_maps"],
            ordering=list(cfg.get("palette") or []),
        )

    assert exc.value.status_code == 409
    assert "fresh white-cap canonical fields" in exc.value.detail


def test_loaded_run_supports_swap_after_save_clear_load(roundtrip_env):
    """save -> clear -> load yields a complete fresh-card cached entry whose
    rehydrated geometry (white-cap map + image_domain dims) lets swap
    instructions generate. Fallback oracle — see module docstring for why the
    full 3MF mesh compare is not used here.
    """
    env = roundtrip_env
    _seed_export_valid_solve("orig")

    # 1. Save the original, then clear the cache (simulate a fresh session).
    save_id = env.client.post("/api/runs/save", json={"card_id": "orig"}).json()["save_id"]
    env.server.session["solve_cache"].clear()
    assert "orig" not in env.server.session["solve_cache"]

    # 2. Load it back -> fresh loaded-* card, complete cached entry.
    loaded = env.client.post("/api/runs/load", json={"save_id": save_id}).json()
    loaded_card = loaded["card_id"]
    assert loaded_card and loaded_card != "orig"
    entry = env.server.session["solve_cache"][loaded_card]["solve"]
    assert entry["status"] == "complete"
    # Geometry-bearing fields rehydrated (these are exactly what swap/export read).
    assert entry["image_domain_width_mm"] == 100.0
    assert entry["image_domain_height_mm"] == 80.0
    assert loaded["result"]["image_domain_width_mm"] == 100.0
    assert loaded["result"]["image_domain_height_mm"] == 80.0
    assert MapKey.WHITE_CAP in entry["thickness_maps"]
    assert WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY in entry["export_maps"]
    assert WHITE_CAP_FIELD_TARGET_METADATA_KEY in entry["export_metadata"]

    # 3. Swap instructions succeed on the LOADED card (proves white-cap map +
    #    image_domain dims rehydrated; raises HTTP 500/409 otherwise).
    swap = _build_export_swap_payload(server, loaded_card)
    assert "instructions" in swap and "groups" in swap and "gcode" in swap
    assert isinstance(swap["groups"], list)


def test_stale_phantom_archive_loads_reexports_and_resaves_canonically(roundtrip_env):
    """Old no-op keys migrate at load boundaries without rewriting the source ZIP."""
    env = roundtrip_env
    _seed_export_valid_solve("phantom-orig")
    env.server.session["solve_cache"]["phantom-orig"]["solve"]["export_maps"][
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    ][:] = 0.56
    save_id = env.client.post(
        "/api/runs/save", json={"card_id": "phantom-orig"}
    ).json()["save_id"]

    parsed = run_archive.read_run_archive(run_store.read_zip_bytes(save_id))
    phantom = _stale_phantom_values()
    stale_run_json = copy.deepcopy(parsed.run_json)
    stale_run_json["config"].update(phantom)
    stale_run_json["run_metadata"] = {
        "config": {**stale_run_json["config"], **phantom},
        "recipe_snapshot": {
            "config": {**stale_run_json["config"], **phantom},
            "profile_snapshot": {
                "settings": {
                    **phantom,
                    "source_resample_kernel": "area",
                    "preprocessing_params": {"b3_tv_flatten": {"tv_weight": 0.13}},
                },
                "modules": {"b3_tv_flatten": True},
            },
        },
        "solve_start_diagnostics": {
            "resolved_settings": {**stale_run_json["config"], **phantom}
        },
    }
    stale_run_json.setdefault("result", {})["solve_start_diagnostics"] = {
        "resolved_settings": {**stale_run_json["config"], **phantom}
    }
    stale_cache_metadata = {
        "config": {**stale_run_json["config"], **phantom},
        "recipe_snapshot": copy.deepcopy(
            stale_run_json["run_metadata"]["recipe_snapshot"]
        ),
        "solve_start_diagnostics": copy.deepcopy(
            stale_run_json["run_metadata"]["solve_start_diagnostics"]
        ),
    }
    stale_cache_files = dict(parsed.run_cache_files)
    stale_cache_files["run.json"] = json.dumps(stale_cache_metadata).encode("utf-8")
    stale_bytes = run_archive.pack_run_archive(
        run_json=stale_run_json,
        thickness_arrays=parsed.thickness_arrays,
        image_bytes=parsed.image_bytes,
        image_name=parsed.image_name,
        solve_state=parsed.solve_state,
        run_cache_files=stale_cache_files,
    )
    sidecar = next(row for row in run_store.list_saves() if row["save_id"] == save_id)
    run_store.write_save(save_id, stale_bytes, sidecar)
    source_before = run_store.read_zip_bytes(save_id)

    settings_response = env.client.post(
        "/api/runs/settings", json={"save_id": save_id, "tier": "saved"}
    )
    assert settings_response.status_code == 200
    settings = settings_response.json()
    _assert_no_phantom_settings(settings["config"])
    for carrier in _archive_settings_carriers(
        {
            "config": settings["config"],
            "run_metadata": settings["run_metadata"],
            "result": settings["result"],
        }
    ):
        _assert_no_phantom_settings(carrier)
    profile = settings["run_metadata"]["recipe_snapshot"]["profile_snapshot"]
    assert profile["settings"]["source_resample_kernel"] == "area"
    assert profile["settings"]["preprocessing_params"] == {
        "b3_tv_flatten": {"tv_weight": 0.13}
    }
    assert profile["modules"] == {"b3_tv_flatten": True}
    assert run_store.read_zip_bytes(save_id) == source_before

    env.server.session["solve_cache"].clear()
    loaded_response = env.client.post(
        "/api/runs/load", json={"save_id": save_id, "tier": "saved"}
    )
    assert loaded_response.status_code == 200
    loaded = loaded_response.json()
    loaded_card = loaded["card_id"]
    _assert_no_phantom_settings(loaded["config"])
    for carrier in _archive_settings_carriers(
        {
            "config": loaded["config"],
            "run_metadata": loaded["run_metadata"],
            "result": loaded["result"],
        }
    ):
        _assert_no_phantom_settings(carrier)
    cached = env.server.session["solve_cache"][loaded_card]
    _assert_no_phantom_settings(cached["config"])

    bundle = env.server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id=loaded_card,
        solve=cached["solve"],
        cfg=cached["config"],
        thickness_maps=cached["solve"]["thickness_maps"],
        ordering=list(cached["config"].get("palette") or []),
    )
    assert (bundle / "arrays.npz").exists()
    assert "gcode" in _build_export_swap_payload(env.server, loaded_card)

    started = env.client.post(
        "/api/export/files/start",
        json={
            "card_id": loaded_card,
            "geometry_source": "field_derived",
            "field_scale": 2,
            "output_format": "stls",
            "validate_written_meshes": False,
        },
    )
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        export_status = env.client.get("/api/export/files/status").json()
        assert export_status["job_id"] == job_id
        if export_status["status"] in {"complete", "error", "cancelled"}:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("background re-export did not reach a terminal state")
    assert export_status["status"] == "complete", export_status
    assert export_status["result"]["files"]

    resaved_id = env.client.post(
        "/api/runs/save", json={"card_id": loaded_card, "label": "Canonical"}
    ).json()["save_id"]
    resaved = run_archive.read_run_archive(run_store.read_zip_bytes(resaved_id))
    for carrier in _archive_settings_carriers(resaved.run_json):
        _assert_no_phantom_settings(carrier)
    embedded = json.loads(resaved.run_cache_files["run.json"])
    for carrier in (
        embedded.get("config") or {},
        (embedded.get("recipe_snapshot") or {}).get("config") or {},
        ((embedded.get("recipe_snapshot") or {}).get("profile_snapshot") or {}).get(
            "settings"
        )
        or {},
        (embedded.get("solve_start_diagnostics") or {}).get("resolved_settings") or {},
    ):
        _assert_no_phantom_settings(carrier)
    assert run_store.read_zip_bytes(save_id) == source_before


def test_loaded_run_preserves_banded_swap_plan_after_save_clear_load(roundtrip_env):
    env = roundtrip_env
    grouping = _seed_banded_export_valid_solve("banded-orig")

    save_id = env.client.post(
        "/api/runs/save",
        json={"card_id": "banded-orig"},
    ).json()["save_id"]
    archived = run_archive.read_run_archive(run_store.read_zip_bytes(save_id))
    assert archived.run_json["result"]["staged_metrics"]["swap_grouping"] == grouping
    assert archived.run_json["config"]["min_cap_layers"] == 1

    env.server.session["solve_cache"].clear()
    loaded = env.client.post(
        "/api/runs/load",
        json={"save_id": save_id},
    ).json()
    loaded_card = loaded["card_id"]
    cached = env.server.session["solve_cache"][loaded_card]
    loaded_solve = cached["solve"]
    loaded_cfg = cached["config"]
    assert loaded_cfg["min_cap_layers"] == 1

    assert env.server._swap_grouping_from_solve(loaded_solve) == grouping
    swap = _build_export_swap_payload(env.server, loaded_card)
    assert swap["banded"] is True
    assert swap["pause_z_mm"] == [0.36]
    assert [item["filaments"] for item in swap["groups"]] == grouping["groups"]
    assert "M600" in swap["gcode"]

    bundle = env.server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id=loaded_card,
        solve=loaded_solve,
        cfg=loaded_cfg,
        thickness_maps=loaded_solve["thickness_maps"],
        ordering=list(loaded_cfg["palette"]),
    )
    template = json.loads((bundle / "run_template.json").read_text(encoding="utf-8"))
    replay = json.loads(
        (bundle / "prediction_replay_metadata.json").read_text(encoding="utf-8")
    )
    assert template["swap_grouping"] == grouping
    assert replay["swap_grouping"] == grouping


def test_save_clear_load_roundtrips_via_disk_archive(roundtrip_env):
    """The archive on disk is a self-contained zip that reloads independent of
    the in-RAM original (read the zip bytes from the store, clear, reload)."""
    env = roundtrip_env
    _seed_export_valid_solve("orig")
    save_id = env.client.post("/api/runs/save", json={"card_id": "orig"}).json()["save_id"]
    zip_bytes = run_store.read_zip_bytes(save_id)
    assert zip_bytes[:2] == b"PK"

    env.server.session["solve_cache"].clear()
    loaded_card = env.client.post(
        "/api/runs/load", json={"save_id": save_id}
    ).json()["card_id"]
    # The whole run-cache subtree was restored under the fresh card.
    assert (data_paths.RUN_CACHE_DIR / loaded_card / "predicted.png").exists()
    # Swap still works on the disk-reloaded run.
    swap = _build_export_swap_payload(server, loaded_card)
    assert swap["gcode"] is not None
