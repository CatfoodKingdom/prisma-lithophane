"""Frontend payload identity contract for the solve-status result (Task 5.1).

The typed ``thickness_maps`` container must not change the webapp payload. This
drives ``/api/solve/start`` with a synthetic result whose ``thickness_maps`` is a
real ``ThicknessMaps`` holding the white-cap reserved sentinels (cap / boundary
cap / detail cap) plus a dynamic filament id. Task 5.4 moved ``__de__`` and
``__gamut_mask__`` out of ``thickness_maps`` into ``diagnostics`` on the webapp
path, so the fake result carries those via its ``de_map`` / ``gamut_mask``
accessors and a ``diagnostics`` dict instead. The test pins:

* the full top-level key set of ``solve["result"]`` (frozen baseline),
* the critical nested URL/bin/filament fields the UI depends on,
* that no ``MapKey`` enum repr leaks into any payload key or string value.

This is separate from the image-hash oracle: the hash protects solver output,
this protects the UI contract. ``thickness_maps`` reserved keys are *lookup*
keys that drive fields like ``cap_map_url`` — they are never themselves JSON
keys, while dynamic filament ids ARE serialized as keys under
``filament_bin_urls``. Both contracts are asserted here.
"""
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
from thickness_maps import MapKey, ThicknessMaps


_SESSION_TEMPLATE = copy.deepcopy(server.session)

_FILAMENT_ID = "demo-filament"

# Frozen top-level key set of solve["result"]. Captured from the behavior-
# preserving tree (Task 5.1 changed only the keys used to *look up* arrays, not
# any emitted payload field name); the image-hash oracle confirms the solver
# output is byte-identical. Any add/drop here is a UI-contract regression.
_EXPECTED_RESULT_KEYS = frozenset({
    "card_id", "diagnostic_palette_version", "mean_de", "source_rms_de", "max_de", "de_scale_max", "n_oog",
    "total_pixels", "coverage_pct", "image_w", "image_h",
    "image_domain_width_mm", "image_domain_height_mm", "max_height",
    "predicted_url", "predicted_appearance_url",
    "predicted_color_only_appearance_url", "de_map_url",
    "de_map_perceptual_url", "de_map_maxset_url", "palette_fit_url",
    "solver_loss_map_url", "solver_loss_raw_url", "solver_loss_scale_max",
    "palette_fit_rms_de", "solver_loss_rms_de", "de_raw_url", "source_url",
    "target_transmission_url", "reference_url", "cap_map_url",
    "cap_map_active_px", "cap_map_max_d", "cap_map_volume_mm3", "cap_map_contour_bin_url",
    "boundary_cap_map_url", "boundary_cap_map_active_px",
    "boundary_cap_map_max_d", "boundary_cap_map_volume_mm3", "boundary_cap_contour_bin_url",
    "detail_cap_map_url", "detail_cap_map_active_px", "detail_cap_map_max_d",
    "detail_cap_map_volume_mm3", "detail_cap_contour_bin_url", "white_cap_scale_max_d",
    "cap_surface_height_url", "cap_surface_height_max_d",
    "cap_surface_height_contour_bin_url", "boundary_cap_surface_height_url",
    "boundary_cap_surface_height_max_d",
    "boundary_cap_surface_height_contour_bin_url",
    "detail_cap_surface_height_url", "detail_cap_surface_height_max_d",
    "detail_cap_surface_height_contour_bin_url", "color_ceiling_url",
    "total_surface_url", "color_ceiling_max_d", "total_surface_max_d",
    "color_ceiling_bin_url", "color_ceiling_contour_bin_url",
    "total_surface_bin_url", "total_surface_contour_bin_url",
    "cap_height_bin_url", "boundary_cap_height_bin_url",
    "detail_cap_height_bin_url", "filament_bin_urls",
    "explorer_stack_label_bin_url", "explorer_stack_table",
    "color_recipe_breakdown_json_url", "color_recipe_breakdown_csv_url",
    "color_recipe_breakdown_cookbook_url",
    # Additional solve-result payload fields (emitted past the cap/diagnostic
    # block); not thickness_maps-derived but part of the frozen UI contract.
    "filament_maps", "debug_map_urls", "cap_quality",
    "staged_metrics", "material_exposure_audit", "white_cap_exposure_safe",
    "color_recipe_breakdown_summary", "explorer_base_filament_id",
    "explorer_cap_filament_id", "explorer_base_thickness_mm",
    "solve_start_diagnostics",
})

# Fields the UI directly binds; must be present and non-None for a result that
# carries boundary/detail caps and at least one filament.
_CRITICAL_NON_NULL = (
    "diagnostic_palette_version",
    "predicted_url",
    "de_map_url",
    "de_map_perceptual_url",
    "de_map_maxset_url",
    "de_raw_url",
    "cap_map_url",
    "cap_height_bin_url",
    "boundary_cap_map_url",
    "boundary_cap_height_bin_url",
    "detail_cap_map_url",
    "detail_cap_height_bin_url",
    "color_ceiling_url",
    "total_surface_url",
)

_STRUCTURAL_SPLIT_DEBUG_KEYS = (
    "stage4_boundary_minimal_floor_mm",
    "stage4_appearance_desired_final_cap_mm",
    "stage4_boundary_structural_cap_mm",
    "stage4_detail_residual_from_appearance_target_mm",
    "stage4_final_target_equivalence_delta_mm",
)

_RETIRED_PRINTABILITY_TERMS = (
    "preferred_line_length",
    "printability_preferred_line_length",
    "short_length_preferred",
    "blueprint_printability_preferred",
    "soft_warn",
    "soft_short",
)


class _ImmediateThread:
    def __init__(self, target, daemon=True):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _write_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")


def _make_fake_result() -> SimpleNamespace:
    h, w = 4, 4
    thickness_map = np.full((h, w), 0.20, dtype=np.float32)
    cap_map = np.full((h, w), 0.16, dtype=np.float32)
    boundary_cap = np.full((h, w), 0.08, dtype=np.float32)
    detail_cap = np.full((h, w), 0.08, dtype=np.float32)
    de_map = np.full((h, w), 0.01, dtype=np.float32)
    gamut_mask = np.zeros((h, w), dtype=np.uint8)

    # Real typed container with the white-cap reserved sentinels + a dynamic
    # filament id. This is the heart of the test: the materialization path reads
    # these via MapKey and must still emit the legacy payload. Task 5.4: __de__
    # and __gamut_mask__ are no longer thickness_maps keys on the webapp path —
    # they live in diagnostics and reach the payload via the de_map/gamut_mask
    # accessors (set on the fake result below).
    maps = ThicknessMaps()
    maps[_FILAMENT_ID] = thickness_map
    maps[MapKey.WHITE_CAP] = cap_map
    maps[MapKey.WHITE_BOUNDARY_CAP] = boundary_cap
    maps[MapKey.WHITE_DETAIL_CAP] = detail_cap

    stats = SimpleNamespace(
        mean_de=0.01,
        max_de=0.02,
        source_rms_de=0.015,
        n_out_of_gamut=0,
        total_pixels=h * w,
        coverage_pct=100.0,
        image_w=w,
        image_h=h,
        max_height=0.28,
        per_filament=[
            SimpleNamespace(
                filament_id=_FILAMENT_ID,
                active_pixels=h * w,
                mean_thickness=0.20,
                max_thickness=0.20,
            )
        ],
    )
    structural_split_debug = {
        key: np.full((h, w), 0.08, dtype=np.float32)
        for key in _STRUCTURAL_SPLIT_DEBUG_KEYS
    }

    return SimpleNamespace(
        thickness_maps=maps,
        color_profiles={},
        wb_profile={},
        wc_profile={},
        grouping=None,
        image_domain_width_mm=float(w),
        image_domain_height_mm=float(h),
        solved_plan=None,
        de_map=de_map,
        cap_map=cap_map,
        boundary_cap=boundary_cap,
        detail_cap=detail_cap,
        gamut_mask=gamut_mask,
        # Task 5.4: webapp results expose DE/GAMUT via diagnostics (plain-str keys).
        diagnostics={"__de__": de_map, "__gamut_mask__": gamut_mask.astype(np.float32)},
        solver_loss_map=None,
        palette_fit_rms_de=None,
        solver_loss_rms_de=None,
        debug_maps=structural_split_debug,
        cap_quality={},
        stats=stats,
        predict_image=lambda: np.zeros((h, w, 3), dtype=np.uint8),
        # Phase 2: the color-only render (cap omitted) for the recipe viewer.
        # A distinct constant keeps it byte-different from the full prediction so
        # the cap-omission contract is observable end-to-end.
        predict_image_color_only=lambda: np.full((h, w, 3), 64, dtype=np.uint8),
    )


@pytest.fixture
def solve_env(tmp_path, monkeypatch):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")

    image_path = images_dir / "solve-input.png"
    Image.new("RGB", (4, 4), "white").save(image_path)

    monkeypatch.setattr(server, "_IMAGES_DIR", images_dir)
    monkeypatch.setattr(server, "_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server, "load_image", lambda *a, **k: np.zeros((4, 4, 3), dtype=np.uint8))
    monkeypatch.setattr(server, "apply_adjustments", lambda img, *a, **k: img)

    # Stub every raster writer to a tiny placeholder so the route runs fast and
    # filesystem-light. The white-cap part/height writers return real stats
    # dicts so the boundary/detail payload branches stay populated.
    monkeypatch.setattr(server, "_save_de_map", lambda _a, p: _write_placeholder(p))
    monkeypatch.setattr(server, "_save_de_map_scaled", lambda _a, p, **k: _write_placeholder(p))
    monkeypatch.setattr(server, "_save_cap_height_map", lambda _a, p, **k: _write_placeholder(p))
    monkeypatch.setattr(server, "_save_overlay_map", lambda _a, p: _write_placeholder(p))
    monkeypatch.setattr(server, "_save_de_raw", lambda _a, p, **k: _write_placeholder(p))
    monkeypatch.setattr(server, "_save_surface_blob", lambda _a, p: _write_placeholder(p))

    def _fake_part_map(arr, path, **kwargs):
        if arr is None:
            return None
        _write_placeholder(path)
        return {"active_px": 4, "max_d": 0.08}

    def _fake_masked_height(arr, mask, path, **kwargs):
        if arr is None or mask is None:
            return None
        _write_placeholder(path)
        return {"max_d": 0.08}

    monkeypatch.setattr(server, "_save_white_cap_part_map", _fake_part_map)
    monkeypatch.setattr(server, "_save_masked_white_cap_height_map", _fake_masked_height)
    monkeypatch.setattr(server, "_save_masked_contour_blob",
                        lambda *a, **k: True)

    captured = {}

    def fake_solve_full(img, sc, progress=None, modules_path=None):
        captured["solve_config"] = sc
        return _make_fake_result()

    monkeypatch.setattr(server, "solve_full", fake_solve_full)
    # Reassign the module-global session via monkeypatch so pytest restores the
    # original after the test — direct assignment would leak into other tests.
    monkeypatch.setattr(server, "session", copy.deepcopy(_SESSION_TEMPLATE))
    server.session["config"].update({
        "image_path": image_path.name,
        "palette": [_FILAMENT_ID],
    })

    return SimpleNamespace(client=TestClient(server.app), output_dir=output_dir)


def _run_solve(client) -> dict:
    start = client.post("/api/solve/start", json={"card_id": "contract_run"})
    assert start.status_code == 200, start.text
    result = server.session["solve"]["result"]
    assert result is not None
    return result


def test_solve_result_top_level_key_set_is_frozen(solve_env):
    result = _run_solve(solve_env.client)
    assert set(result.keys()) == set(_EXPECTED_RESULT_KEYS), (
        "solve result top-level key set drifted from the frozen UI contract: "
        f"added={set(result) - set(_EXPECTED_RESULT_KEYS)}, "
        f"dropped={set(_EXPECTED_RESULT_KEYS) - set(result)}"
    )


def test_critical_nested_fields_present_and_non_null(solve_env):
    result = _run_solve(solve_env.client)
    for key in _CRITICAL_NON_NULL:
        assert key in result, f"missing critical payload field {key!r}"
        assert result[key] is not None, f"critical payload field {key!r} is None"


def test_solve_result_exposes_content_domain_physical_dimensions(solve_env):
    result = _run_solve(solve_env.client)
    assert result["image_domain_width_mm"] == pytest.approx(4.0)
    assert result["image_domain_height_mm"] == pytest.approx(4.0)


def test_filament_bin_urls_keyed_by_dynamic_filament_id(solve_env):
    result = _run_solve(solve_env.client)
    bin_urls = result["filament_bin_urls"]
    assert isinstance(bin_urls, dict)
    assert _FILAMENT_ID in bin_urls
    # The dynamic filament id is serialized as a plain-string JSON key.
    assert all(isinstance(k, str) for k in bin_urls)


def test_filament_map_volume_integrates_solve_grid_thickness(solve_env):
    result = _run_solve(solve_env.client)
    [filament_map] = result["filament_maps"]
    # The fake 4x4 map is 0.20 mm everywhere over a 4x4 mm image domain:
    # 16 pixels * 0.20 mm * 1 mm^2/pixel = 3.2 mm^3.
    assert filament_map["volume_mm3"] == pytest.approx(3.2)


def test_white_cap_map_volumes_integrate_solve_grid_thickness(solve_env):
    result = _run_solve(solve_env.client)
    # The same 4x4 mm image domain gives a 1 mm^2 pixel area. The fixture's
    # total cap is 0.16 mm/pixel and each component is 0.08 mm/pixel.
    assert result["cap_map_volume_mm3"] == pytest.approx(2.56)
    assert result["boundary_cap_map_volume_mm3"] == pytest.approx(1.28)
    assert result["detail_cap_map_volume_mm3"] == pytest.approx(1.28)


def test_structural_split_debug_maps_are_exposed(solve_env):
    result = _run_solve(solve_env.client)
    debug_urls = result["debug_map_urls"]
    out_dir = server._current_out_dir("contract_run")
    for key in _STRUCTURAL_SPLIT_DEBUG_KEYS:
        assert key in debug_urls
        assert debug_urls[key].startswith(f"/api/run-cache/files/{key}.png")
        assert (out_dir / f"{key}.png").exists()


def test_payload_contains_no_retired_preferred_length_or_soft_warning_keys(solve_env):
    result = _run_solve(solve_env.client)
    dumped = json.dumps(result, default=str).lower()
    for term in _RETIRED_PRINTABILITY_TERMS:
        assert term not in dumped


def test_color_only_appearance_emitted_and_differs_from_full(solve_env):
    """Phase 2: the color-only appearance PNG is baked and is distinct from the
    full appearance (the white cap was actually omitted)."""
    import numpy as _np
    from PIL import Image as _Image

    result = _run_solve(solve_env.client)
    assert result["predicted_color_only_appearance_url"] is not None

    # Locate the run-cache out dir for this card_id and read both PNGs.
    out_dir = server._current_out_dir("contract_run")
    full_path = out_dir / "predicted_appearance.png"
    color_only_path = out_dir / "predicted_color_only_appearance.png"
    raw_color_only = out_dir / "predicted_color_only.png"
    assert full_path.exists(), "full appearance PNG missing"
    assert color_only_path.exists(), "color-only appearance PNG missing"
    assert raw_color_only.exists(), "raw color-only model-domain PNG missing"

    full = _np.asarray(_Image.open(full_path).convert("RGB"))
    color_only = _np.asarray(_Image.open(color_only_path).convert("RGB"))
    assert full.shape == color_only.shape
    # The fake result returns different model-domain images for full (zeros) vs
    # color-only (64); after F they must remain distinct — proving the
    # color-only render is baked independently, not aliased to the full one.
    assert _np.any(full != color_only), (
        "color-only appearance is identical to the full appearance — "
        "the cap-omitted render was not baked separately"
    )


def test_no_mapkey_enum_repr_leaks_into_payload(solve_env):
    result = _run_solve(solve_env.client)
    # Reserved sentinels are lookup keys, never payload keys/values. A leaked
    # enum repr ("MapKey.WHITE_CAP") would prove a serializer saw the enum.
    dumped = json.dumps(result, default=str)
    assert "MapKey." not in dumped
    for key in result:
        assert type(key) is str
        assert not key.startswith("__")
