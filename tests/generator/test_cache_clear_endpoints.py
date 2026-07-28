"""Safety and lifecycle coverage for the supported /api/cache/clear-all endpoint.

Tests verify:
- clear-all empties CACHE_DIR (runs + luts + auto_runs), clears in-RAM caches, and keeps output.
- It spares the user-curated saved_runs/ (Stage 9c).
- It returns HTTP 409 while a solve, export, or palette-suggestion job is running.
- It cannot reach outside CACHE_DIR (safe_clear_dir enforces the boundary).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import data_paths
import server

_SESSION_TEMPLATE = copy.deepcopy(server.session)

client = TestClient(server.app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_file(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _reset_session() -> None:
    """Restore session to idle / empty state between tests."""
    server.session.update(copy.deepcopy(_SESSION_TEMPLATE))


def test_favicon_request_is_quiet_no_content() -> None:
    """Browsers request /favicon.ico automatically; it should not hit static 404."""
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204
    assert resp.content == b""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_session():
    """Reset the in-RAM session before and after every test."""
    _reset_session()
    server._clear_palette_backend_cache()
    yield
    _reset_session()
    server._clear_palette_backend_cache()


@pytest.fixture()
def cache_dirs(tmp_path, monkeypatch):
    """Redirect all cache dirs (and OUTPUT_DIR) to tmp so we never touch real data."""
    cache = tmp_path / "cache"
    runs = cache / "runs"
    luts = cache / "luts"
    auto_runs = cache / "auto_runs"
    palette_batches = cache / "palette-batches"
    image_imports = cache / "image-imports"
    output = tmp_path / "output"

    for d in (runs, luts, auto_runs, palette_batches, image_imports, output):
        d.mkdir(parents=True)

    # Patch data_paths module-level attrs — the new endpoints read via data_paths.X
    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", runs)
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", luts)
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto_runs)
    monkeypatch.setattr(data_paths, "SOURCE_IMAGE_IMPORT_DIR", image_imports)

    return {
        "cache": cache,
        "runs": runs,
        "luts": luts,
        "auto_runs": auto_runs,
        "palette_batches": palette_batches,
        "image_imports": image_imports,
        "output": output,
    }


# ---------------------------------------------------------------------------
# Tests: happy-path scoping
# ---------------------------------------------------------------------------

def test_ensure_dirs_creates_auto_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "GENERATOR_DATA_DIR", tmp_path / "generator")
    monkeypatch.setattr(data_paths, "CACHE_DIR", tmp_path / "generator" / "cache")
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", tmp_path / "generator" / "cache" / "runs")
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", tmp_path / "generator" / "cache" / "luts")
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "generator" / "cache" / "auto_runs")
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "generator" / "saved_runs")
    monkeypatch.setattr(data_paths, "LOG_DIR", tmp_path / "generator" / "logs")
    monkeypatch.setattr(data_paths, "OUTPUT_DIR", tmp_path / "output" / "lithophanes")

    data_paths.ensure_dirs()

    assert data_paths.AUTO_RUNS_DIR.is_dir()


def test_clear_all_wipes_auto_runs_but_keeps_saved(cache_dirs, tmp_path, monkeypatch):
    """Stage 9c: clear-all sweeps auto_runs/ too; saved_runs/ survives."""
    dirs = cache_dirs
    saved = tmp_path / "saved_runs"
    saved.mkdir()
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved)

    _write_file(dirs["auto_runs"] / "auto-1.zip")
    _write_file(dirs["auto_runs"] / "auto-1.json")
    _write_file(saved / "save-1.zip")

    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200, resp.text

    assert not any(dirs["auto_runs"].iterdir()), "auto_runs/ should be empty after clear-all"
    assert (saved / "save-1.zip").exists(), "saved_runs/ must survive clear-all"


def test_clear_all_clears_luts_too(cache_dirs):
    """clear-all wipes runs and luts; OUTPUT_DIR is untouched."""
    dirs = cache_dirs

    _write_file(dirs["runs"] / "r.json")
    _write_file(dirs["luts"] / "l.lut")
    _write_file(dirs["output"] / "keep.3mf")

    server.session["solve_cache"]["x"] = {"solve": {}, "config": {}}
    with server._PALETTE_BACKEND_CACHE_LOCK:
        server._PALETTE_BACKEND_CACHE[("unit",)] = object()

    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["cleared"] == "all"
    # Cache subdirs emptied
    assert not any(dirs["runs"].iterdir()), "runs/ should be empty after clear-all"
    assert not any(dirs["luts"].iterdir()), "luts/ should be empty after clear-all"
    # output untouched
    assert (dirs["output"] / "keep.3mf").exists(), "output must survive clear-all"
    # in-RAM cache cleared
    assert server.session["solve_cache"] == {}
    assert server._PALETTE_BACKEND_CACHE == {}


def test_clear_all_removes_batch_and_import_work_and_resets_batch(cache_dirs):
    dirs = cache_dirs
    _write_file(dirs["palette_batches"] / "batch-a" / "source.png")
    _write_file(dirs["image_imports"] / ".upload-a.png")
    server.session["palette_batch"].update({
        "status": "complete",
        "job_id": "batch-a",
        "items": [{"result_id": "batch-a-i01"}],
    })

    response = client.post("/api/cache/clear-all")

    assert response.status_code == 200
    assert not any(dirs["palette_batches"].iterdir())
    assert not any(dirs["image_imports"].iterdir())
    assert server.session["palette_batch"]["status"] == "idle"
    assert server.session["palette_batch"]["job_id"] is None
    assert server.session["palette_batch"]["items"] == []


def test_clear_all_removed_count(cache_dirs):
    """'removed' is the sum across the cleared cache subdirs."""
    dirs = cache_dirs
    _write_file(dirs["runs"] / "r1.json")
    _write_file(dirs["luts"] / "l1.lut")
    _write_file(dirs["luts"] / "l2.lut")

    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200
    # 1 run + 2 luts = 3 top-level entries total
    assert resp.json()["removed"] == 3


def test_clear_all_on_empty_dirs_is_ok(cache_dirs):
    """Clearing already-empty dirs returns 200 with removed=0."""
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


def test_clear_all_invalidates_active_private_loaded_source(cache_dirs):
    card_id = "loaded-123"
    source_dir = cache_dirs["runs"] / card_id / server._LOADED_SOURCE_PRIVATE_DIR
    _write_file(source_dir / "source.jpg", b"private")
    server.session["config"]["image_path"] = "source.jpg"
    server.session["config"]["image_source_ref"] = f"loaded-run:{card_id}"
    server.session["solve_cache"][card_id] = {
        "config": {},
        "solve": {
            "_loaded_source": {
                "relative_path": f"{server._LOADED_SOURCE_PRIVATE_DIR}/source.jpg",
            },
        },
    }

    response = client.post("/api/cache/clear-all")

    assert response.status_code == 200
    body = response.json()
    assert body["active_image_cleared"] is True
    assert body["cleared_source_ref"] == f"loaded-run:{card_id}"
    assert body["config"]["image_path"] is None
    assert body["config"]["image_source_ref"] is None
    assert server.session["config"]["image_path"] is None
    assert server.session["config"]["image_source_ref"] is None
    assert server.session["solve_cache"] == {}


# ---------------------------------------------------------------------------
# Tests: 409 guard — all three living job types
# ---------------------------------------------------------------------------

def test_clear_refused_while_solve_running(cache_dirs):
    """The endpoint returns 409 when a solve job is active."""
    server.session["solve"]["status"] = "running"
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 409


def test_clear_refused_while_export_running(cache_dirs):
    """The endpoint returns 409 when an export job is active."""
    server.session["export"]["status"] = "running"
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 409


def test_clear_refused_while_suggest_running(cache_dirs):
    """The endpoint returns 409 when a palette-suggestion job is active."""
    server.session["suggest"]["status"] = "running"
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 409


def test_clear_refused_while_palette_batch_running(cache_dirs):
    server.session["palette_batch"]["status"] = "running"
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 409


@pytest.mark.parametrize("status", ["idle", "complete", "error", "cancelled"])
def test_clear_allowed_when_jobs_not_running(cache_dirs, status):
    """The endpoint succeeds for every non-running status value."""
    server.session["solve"]["status"] = status
    server.session["export"]["status"] = status
    server.session["suggest"]["status"] = status
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: synchronous status assignment (TOCTOU fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job", ["solve", "export", "suggest"])
def test_assert_no_active_job_blocks_when_status_is_running(cache_dirs, job):
    """_assert_no_active_job raises 409 as soon as status is 'running'.

    The solve, export, and palette-suggestion start handlers set status='running'
    synchronously (before spawning the background thread), so the clear
    endpoints see the guard correctly without a race.  This test confirms the
    mechanism: setting session[job][status]='running' by hand (which is exactly
    what the synchronous assignment does) is enough to block the clear.
    """
    server.session[job]["status"] = "running"
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 409


def test_background_export_start_is_409_guarded_against_concurrent_run(cache_dirs):
    """The living start route rejects a second export while one is running."""
    server.session["export"]["status"] = "running"
    resp = client.post("/api/export/files/start", json={})
    assert resp.status_code == 409, (
        "background /api/export/files/start must 409 when an export is already running"
    )
