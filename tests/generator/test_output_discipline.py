"""Stage 9a Slice 2: verify the output/cache split.

Invariants tested:
- A solve writes its diagnostics under RUN_CACHE_DIR (not OUTPUT_DIR).
- Diagnostic URLs use /api/run-cache/files/.
- export_id/OUTPUT_DIR split: make_export_id + _export_out_dir land under OUTPUT_DIR.
- A real export (via TestClient) writes deliverables under OUTPUT_DIR/{export_id}/ and
  the result payload carries export_id + zip_url containing dir={export_id}.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import data_paths
import server
from run_naming import make_export_id

# ── Shared template (same pattern as test_solve_start_diagnostics) ─────────

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
        grouping=None,
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
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_cache_dir = tmp_path / "cache" / "runs"
    run_cache_dir.mkdir(parents=True)
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")

    image_path = images_dir / "solve-input.png"
    image_path.write_bytes(b"not-a-real-png-but-it-exists")

    monkeypatch.setattr(server, "_IMAGES_DIR", images_dir)
    monkeypatch.setattr(server, "_OUTPUT_DIR", output_dir)
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
    # Patch data_paths.RUN_CACHE_DIR so _current_out_dir resolves to tmp
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", run_cache_dir)

    captured = {}

    def fake_solve_full(img, sc, progress=None, modules_path=None):
        captured["image_shape"] = img.shape
        captured["solve_config"] = sc
        captured["modules_path"] = modules_path
        return _make_fake_result(sc.palette)

    monkeypatch.setattr(server, "solve_full", fake_solve_full)
    fresh_session = copy.deepcopy(_SESSION_TEMPLATE)
    fresh_session["config"].update({
        "image_path": image_path.name,
        "palette": ["demo-filament"],
    })
    monkeypatch.setattr(server, "session", fresh_session)

    return SimpleNamespace(
        client=TestClient(server.app),
        output_dir=output_dir,
        run_cache_dir=run_cache_dir,
        captured=captured,
        image_path=image_path,
        modules_path=modules_path,
    )


# ── Unit tests: make_export_id + _export_out_dir ───────────────────────────

def test_make_export_id_returns_slug_under_output_root(tmp_path):
    """make_export_id produces a Windows-safe slug and doesn't create a folder."""
    export_id = make_export_id("my-photo.JPG", tmp_path, timestamp="20260615-143022")
    assert export_id  # non-empty
    assert "/" not in export_id and "\\" not in export_id
    # No folder created yet — make_export_id only produces the id
    assert not (tmp_path / export_id).exists()


def test_make_export_id_collision_avoidance(tmp_path):
    """Two exports in the same second return distinct ids."""
    first = make_export_id("photo.jpg", tmp_path, timestamp="20260615-143022")
    # Simulate first export dir existing
    (tmp_path / first).mkdir()
    second = make_export_id("photo.jpg", tmp_path, timestamp="20260615-143022")
    assert second != first
    assert second.endswith("-2")


def test_export_out_dir_is_under_output_dir(tmp_path, monkeypatch):
    """_export_out_dir resolves under _OUTPUT_DIR without creating residue."""
    monkeypatch.setattr(server, "_OUTPUT_DIR", tmp_path)
    export_id = "my-export-001"
    out = server._export_out_dir(export_id)
    assert out == tmp_path / export_id
    assert not out.exists()


def test_missing_export_downloads_do_not_create_requested_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_OUTPUT_DIR", tmp_path)
    client = TestClient(server.app)

    assert client.get("/api/export/files-zip?dir=missing-export").status_code == 404
    assert client.get(
        "/api/export/files/export_manifest.json?dir=missing-export"
    ).status_code == 404
    assert not (tmp_path / "missing-export").exists()


# ── Solve discipline: diagnostics go to RUN_CACHE_DIR, not OUTPUT_DIR ──────

def test_solve_writes_to_run_cache_not_output(solve_env):
    """A plain solve must not create any files under OUTPUT_DIR."""
    client = solve_env.client
    resp = client.post("/api/solve/start", json={"card_id": "cache_only_run"})
    assert resp.status_code == 200

    # run cache dir gets the solve artefacts
    run_dir = solve_env.run_cache_dir / "cache_only_run"
    assert run_dir.exists(), "Solve did not create a run cache dir"

    # OUTPUT_DIR must be empty — no run.json or other solve artefacts there
    output_files = list(solve_env.output_dir.rglob("run.json"))
    assert output_files == [], (
        f"Solve wrote run.json to OUTPUT_DIR: {output_files}"
    )


def test_solve_diagnostic_urls_use_run_cache_route(solve_env):
    """Solve result URLs for diagnostics must start with /api/run-cache/files/."""
    client = solve_env.client
    resp = client.post("/api/solve/start", json={"card_id": "url_check_run"})
    assert resp.status_code == 200

    result = server.session["solve"]["result"]
    # Check at least one known diagnostic URL if present
    for key in ("predicted_url", "de_map_url", "overlay_url"):
        url = result.get(key)
        if url:
            assert url.startswith("/api/run-cache/files/"), (
                f"{key}={url!r} should start with /api/run-cache/files/"
            )


# ── Export discipline: deliverables go to OUTPUT_DIR/{export_id}/ ──────────

def _make_export_bundle(tmp_path: Path, palette: list[str]) -> SimpleNamespace:
    """Minimal fake export bundle for monkeypatching export_solve_bundle."""
    from types import SimpleNamespace
    stls_dir = tmp_path / "stls"
    stls_dir.mkdir(parents=True, exist_ok=True)
    stl_file = stls_dir / "layer_0.stl"
    stl_file.write_bytes(b"solid fake\nendsolid fake")
    manifest = {
        "filaments": palette,
        "objects": [{"object_key": "layer_0", "path": str(stl_file)}],
    }
    manifest_path = tmp_path / "export_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    obj = SimpleNamespace(
        object_key="layer_0",
        material_key=palette[0],
        role="filament",
        mesh_style="stl",
    )
    return SimpleNamespace(
        output_paths={"layer_0": stl_file},
        bundle=SimpleNamespace(
            objects=[obj],
            quality={"layer_0": {"n_faces": 10, "is_watertight": True, "has_holes": False, "n_pinch_edges": 0}},
        ),
        manifest_path=manifest_path,
        manifest=manifest,
        progress_events=[],
    )


def test_export_writes_deliverables_to_output_dir_not_cache(tmp_path, monkeypatch):
    """After export, deliverables land under OUTPUT_DIR/{export_id}/, not in RUN_CACHE_DIR."""
    import copy

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    run_cache_dir = tmp_path / "cache" / "runs"
    run_cache_dir.mkdir(parents=True)

    image_path = images_dir / "photo.png"
    image_path.write_bytes(b"fake")

    monkeypatch.setattr(server, "_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(server, "_IMAGES_DIR", images_dir)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", run_cache_dir)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)

    palette = ["demo-filament"]
    fake_result_ns = None

    def fake_export_bundle(bundle_path, out_dir, **kwargs):
        nonlocal fake_result_ns
        fake_result_ns = _make_export_bundle(out_dir, palette)
        # Copy files into out_dir as the real export would
        return fake_result_ns

    monkeypatch.setattr(server, "export_solve_bundle", fake_export_bundle)

    # Also patch _materialize_post_solve_export_bundle_from_cached_solve
    monkeypatch.setattr(
        server,
        "_materialize_post_solve_export_bundle_from_cached_solve",
        lambda **kwargs: tmp_path / "bundle",
    )
    (tmp_path / "bundle").mkdir()

    # Seed a completed solve in the session (use monkeypatch so session is restored after test)
    seeded_session = copy.deepcopy(_SESSION_TEMPLATE)
    seeded_session["config"].update({
        "image_path": image_path.name,
        "palette": palette,
        "palette_label": "Test Palette",
    })
    fp_key = "solve_owned_fingerprint"
    seeded_session["solve"].update({
        "status": "complete",
        "thickness_maps": {
            palette[0]: np.full((4, 4), 0.20, dtype=np.float32),
            # Real solves always include a white-cap map (mandatory cap); the
            # export/swap path requires it, so the fake solve must carry one too.
            "__white_cap__": np.full((4, 4), 0.20, dtype=np.float32),
        },
        fp_key: server._solve_owned_fingerprint(seeded_session["config"]),
        "card_id": None,
        # Solve-owned physical domain dims (required by the export/swap path).
        "image_domain_width_mm": 20.0,
        "image_domain_height_mm": 20.0,
    })
    monkeypatch.setattr(server, "session", seeded_session)

    client = TestClient(server.app)
    resp = client.post("/api/export/files", json={
        "geometry_source": "field_derived",
        "field_scale": 4,
        "output_format": "stls",
        "validate_written_meshes": False,
    })
    assert resp.status_code == 200, resp.text

    data = resp.json()

    # export_id must be present in the response
    assert "export_id" in data, "Response missing export_id"
    export_id = data["export_id"]
    assert export_id  # non-empty

    # zip_url must reference export_id, not card_id
    zip_url = data.get("zip_url", "")
    assert f"dir={export_id}" in zip_url, (
        f"zip_url {zip_url!r} does not contain dir={export_id}"
    )

    # manifest_url must reference export_id
    manifest_url = data.get("manifest_url", "")
    assert f"dir={export_id}" in manifest_url, (
        f"manifest_url {manifest_url!r} does not contain dir={export_id}"
    )

    # Each file URL must reference export_id
    for f in data.get("files", []):
        url = f.get("url", "")
        assert f"dir={export_id}" in url, (
            f"file url {url!r} does not contain dir={export_id}"
        )

    # Deliverable folder must be under OUTPUT_DIR, not run cache
    export_out = output_dir / export_id
    assert export_out.is_dir(), (
        f"Expected export dir at {export_out} but it does not exist"
    )
    on_disk_manifest = json.loads((export_out / "export_manifest.json").read_text())
    assert Path(on_disk_manifest["objects"][0]["path"]) == export_out / "stls" / "layer_0.stl"
    assert ".export-stage-" not in json.dumps(on_disk_manifest)
    assert not any(path.name.startswith(".export-stage-") for path in output_dir.iterdir())

    # Nothing should be written under RUN_CACHE_DIR from the export
    cache_stls = list(run_cache_dir.rglob("*.stl"))
    assert cache_stls == [], (
        f"Export wrote STL files into run cache: {cache_stls}"
    )
