"""Living compatibility checks for the quietly retired no-op settings."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace


def _phantom_values(server) -> dict:
    return {
        "smooth_iters": 19,
        "allow_print_despite_hazards": True,
        "detail_cap_pitch_mm": 0.07,
        "v2_cleanup_de_budget": 0.31,
        "v2_enable_cliff_closure": False,
        "v2_enable_cap_topology_cleanup": True,
        "v2_max_cleanup_rounds": 9,
        "v2_full_cap_quality_report": True,
    }


def _assert_no_phantoms(server, mapping: dict) -> None:
    assert server._PHANTOM_CONFIG_FIELDS.isdisjoint(mapping)


def test_phantom_session_submission_is_quiet_and_fingerprint_neutral(monkeypatch):
    import server

    assert set(_phantom_values(server)) == set(server._PHANTOM_CONFIG_FIELDS)
    monkeypatch.setattr(server, "get_active_printer", lambda: {"nozzle": {}})
    monkeypatch.setattr(server, "load_module_state", lambda _path: {})
    monkeypatch.setattr(server, "_ensure_registry_populated", lambda: None)

    original = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
        baseline = deepcopy(server.session["config"])
        response = server.set_config(server.ConfigPayload(**_phantom_values(server)))

        _assert_no_phantoms(server, response["config"])
        _assert_no_phantoms(server, server.session["config"])
        assert server._solve_owned_fingerprint(baseline) == server._solve_owned_fingerprint(
            {**baseline, **_phantom_values(server)}
        )
    finally:
        server.session.clear()
        server.session.update(original)


def test_stale_profile_normalizes_without_rewriting_source_or_losing_live_state(
    tmp_path, monkeypatch
):
    import server

    profiles_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", profiles_dir)
    profiles_dir.mkdir()
    stale = {
        "id": "legacy-profile",
        "kind": "named",
        "name": "Legacy Profile",
        "schema_version": server._SETTINGS_PROFILE_SCHEMA_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "settings": {
            **_phantom_values(server),
            "source_resample_kernel": "area",
            "preprocessing_params": {"b1_printscale_bilateral": {"passes": 2}},
        },
        "modules": {"b1_printscale_bilateral": True},
    }
    source_path = profiles_dir / "legacy-profile.json"
    source_path.write_text(json.dumps(stale, indent=2), encoding="utf-8")
    before = source_path.read_bytes()

    record = server._load_settings_profile_record(source_path, kind_hint="named")

    assert source_path.read_bytes() == before
    _assert_no_phantoms(server, record.settings)
    assert record.settings["source_resample_kernel"] == "area"
    assert record.settings["preprocessing_params"] == {
        "b1_printscale_bilateral": {"passes": 2}
    }
    assert record.modules["b1_printscale_bilateral"] is True

    created = server.create_settings_profile(
        server.SettingsProfilePayload(
            name="New Canonical Profile",
            settings={**_phantom_values(server), "source_resample_kernel": "area"},
            modules={"b1_printscale_bilateral": True},
        )
    )
    saved = next(
        profile for profile in created["profiles"] if profile["name"] == "New Canonical Profile"
    )
    persisted = json.loads(server._settings_profile_path(saved["id"]).read_text(encoding="utf-8"))
    _assert_no_phantoms(server, persisted["settings"])
    assert persisted["settings"]["source_resample_kernel"] == "area"
    assert persisted["modules"]["b1_printscale_bilateral"] is True

    updated = server.update_settings_profile(
        saved["id"],
        server.SettingsProfilePayload(
            name="Updated Canonical Profile",
            settings={
                **_phantom_values(server),
                "source_resample_kernel": "lanczos",
                "preprocessing_params": {
                    "b1_printscale_bilateral": {"passes": 3}
                },
            },
            modules={"b1_printscale_bilateral": False},
        ),
    )
    updated_profile = next(
        profile
        for profile in updated["profiles"]
        if profile["name"] == "Updated Canonical Profile"
    )
    updated_persisted = json.loads(
        server._settings_profile_path(updated_profile["id"]).read_text(encoding="utf-8")
    )
    _assert_no_phantoms(server, updated_persisted["settings"])
    assert updated_persisted["settings"]["source_resample_kernel"] == "lanczos"
    assert updated_persisted["settings"]["preprocessing_params"] == {
        "b1_printscale_bilateral": {"passes": 3}
    }
    assert updated_persisted["modules"]["b1_printscale_bilateral"] is False


def test_archive_sanitizer_is_bounded_and_does_not_mutate_source():
    import server

    phantom = _phantom_values(server)
    source = {
        "config": {**phantom, "layer_height": 0.08},
        "run_metadata": {
            "config": {**phantom, "source_resample_kernel": "area"},
            "recipe_snapshot": {
                "config": {**phantom, "layer_height": 0.12},
                "profile_snapshot": {
                    "settings": {**phantom, "preprocessing_params": {"x": {"v": 1}}},
                    "arbitrary": {"smooth_iters": "keep"},
                },
            },
            "solve_start_diagnostics": {
                "resolved_settings": {**phantom, "layer_height": 0.08},
                "arbitrary": {"detail_cap_pitch_mm": "keep"},
            },
            "arbitrary": {"v2_max_cleanup_rounds": "keep"},
        },
        "result": {
            "solve_start_diagnostics": {
                "resolved_settings": {**phantom, "layer_height": 0.08},
                "arbitrary": {"allow_print_despite_hazards": "keep"},
            },
            "arbitrary": {"smooth_iters": "keep"},
        },
        "arbitrary": {"detail_cap_pitch_mm": "keep"},
    }
    original = deepcopy(source)

    sanitized = server._sanitize_archive_run_json_phantom_fields(source)

    assert source == original
    carriers = [
        sanitized["config"],
        sanitized["run_metadata"]["config"],
        sanitized["run_metadata"]["recipe_snapshot"]["config"],
        sanitized["run_metadata"]["recipe_snapshot"]["profile_snapshot"]["settings"],
        sanitized["run_metadata"]["solve_start_diagnostics"]["resolved_settings"],
        sanitized["result"]["solve_start_diagnostics"]["resolved_settings"],
    ]
    for carrier in carriers:
        _assert_no_phantoms(server, carrier)
    assert sanitized["arbitrary"]["detail_cap_pitch_mm"] == "keep"
    assert sanitized["run_metadata"]["arbitrary"]["v2_max_cleanup_rounds"] == "keep"
    assert (
        sanitized["run_metadata"]["recipe_snapshot"]["profile_snapshot"]["arbitrary"]
        ["smooth_iters"]
        == "keep"
    )
    assert (
        sanitized["result"]["solve_start_diagnostics"]["arbitrary"]
        ["allow_print_despite_hazards"]
        == "keep"
    )


def test_new_run_metadata_drops_phantoms_from_every_settings_carrier():
    import server

    phantom = _phantom_values(server)
    stats = SimpleNamespace(
        mean_de=0.1,
        max_de=0.2,
        n_out_of_gamut=0,
        total_pixels=1,
        coverage_pct=100.0,
        image_w=1,
        image_h=1,
        max_height=0.5,
        per_filament=[],
    )
    metadata = server._build_run_metadata(
        cfg={**server._DEFAULT_CONFIG, **phantom},
        stats=stats,
        profile_ref=None,
        profile_name_at_solve=None,
        is_profile_modified_at_solve=False,
        recipe_snapshot={
            "config": {**phantom, "layer_height": 0.08},
            "profile_snapshot": {"settings": {**phantom, "layer_height": 0.08}},
        },
        solve_start_diagnostics={"resolved_settings": {**phantom, "layer_height": 0.08}},
        card_id="card-1",
    )

    _assert_no_phantoms(server, metadata["config"])
    _assert_no_phantoms(server, metadata["recipe_snapshot"]["config"])
    _assert_no_phantoms(
        server, metadata["recipe_snapshot"]["profile_snapshot"]["settings"]
    )
    _assert_no_phantoms(
        server, metadata["solve_start_diagnostics"]["resolved_settings"]
    )
