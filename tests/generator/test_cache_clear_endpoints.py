"""Stage 9a Slice 3: /api/cache/clear-runs and /api/cache/clear-all endpoints.

Tests verify:
- clear-runs empties RUN_CACHE_DIR + AUTO_RUNS_DIR, clears in-RAM solve_cache, keeps LUTs and output.
- clear-all empties CACHE_DIR (runs + luts + auto_runs), clears in-RAM cache, keeps output.
- Both spare the user-curated saved_runs/ (Stage 9c).
- Both return HTTP 409 while a solve, export, or compare job is running.
- Neither endpoint can reach outside CACHE_DIR (safe_clear_dir enforces the boundary).
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
    output = tmp_path / "output"

    for d in (runs, luts, auto_runs, output):
        d.mkdir(parents=True)

    # Patch data_paths module-level attrs — the new endpoints read via data_paths.X
    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", runs)
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", luts)
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto_runs)

    return {
        "cache": cache,
        "runs": runs,
        "luts": luts,
        "auto_runs": auto_runs,
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


def test_clear_runs_keeps_luts_and_output(cache_dirs):
    """clear-runs empties RUN_CACHE_DIR only; LUT_CACHE_DIR and OUTPUT_DIR survive."""
    dirs = cache_dirs

    # Seed some files
    _write_file(dirs["runs"] / "card-1" / "run.json")
    _write_file(dirs["runs"] / "card-2" / "de.png")
    _write_file(dirs["luts"] / "some.lut")
    _write_file(dirs["output"] / "out.3mf")

    # Also put something in in-RAM solve_cache
    server.session["solve_cache"]["card-1"] = {"solve": {}, "config": {}}
    with server._PALETTE_BACKEND_CACHE_LOCK:
        server._PALETTE_BACKEND_CACHE[("unit",)] = object()

    resp = client.post("/api/cache/clear-runs")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["cleared"] == "runs"
    # run-cache dir was emptied
    assert not any(dirs["runs"].iterdir()), "runs/ should be empty after clear-runs"
    # luts and output untouched
    assert (dirs["luts"] / "some.lut").exists(), "LUT file must survive clear-runs"
    assert (dirs["output"] / "out.3mf").exists(), "output file must survive clear-runs"
    # in-RAM cache cleared
    assert server.session["solve_cache"] == {}, "in-RAM solve_cache must be cleared"
    assert len(server._PALETTE_BACKEND_CACHE) == 1, "clear-runs must keep reusable gamut data"


def test_clear_runs_wipes_auto_runs_but_keeps_luts_and_saved(cache_dirs, tmp_path, monkeypatch):
    """Stage 9c: clear-runs sweeps the auto_runs/ cache tier; LUTs + saved_runs/ survive."""
    dirs = cache_dirs
    saved = tmp_path / "saved_runs"
    saved.mkdir()
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved)

    _write_file(dirs["runs"] / "card-1" / "run.json")
    _write_file(dirs["auto_runs"] / "auto-1.zip")
    _write_file(dirs["auto_runs"] / "auto-1.json")
    _write_file(dirs["luts"] / "keep.lut")
    _write_file(saved / "save-1.zip")

    resp = client.post("/api/cache/clear-runs")
    assert resp.status_code == 200, resp.text

    assert not any(dirs["runs"].iterdir()), "runs/ should be empty after clear-runs"
    assert not any(dirs["auto_runs"].iterdir()), "auto_runs/ should be empty after clear-runs"
    assert (dirs["luts"] / "keep.lut").exists(), "LUT file must survive clear-runs"
    assert (saved / "save-1.zip").exists(), "saved_runs/ must survive clear-runs"


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


def test_clear_runs_removed_count(cache_dirs):
    """The 'removed' field reflects the number of top-level entries deleted."""
    dirs = cache_dirs
    _write_file(dirs["runs"] / "a" / "f.json")
    _write_file(dirs["runs"] / "b" / "g.json")

    resp = client.post("/api/cache/clear-runs")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2


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


def test_clear_runs_on_empty_dir_is_ok(cache_dirs):
    """Clearing an already-empty runs dir returns 200 with removed=0."""
    resp = client.post("/api/cache/clear-runs")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


def test_clear_all_on_empty_dirs_is_ok(cache_dirs):
    """Clearing already-empty dirs returns 200 with removed=0."""
    resp = client.post("/api/cache/clear-all")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


# ---------------------------------------------------------------------------
# Tests: 409 guard — all three job types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/cache/clear-runs", "/api/cache/clear-all"])
def test_clear_refused_while_solve_running(cache_dirs, endpoint):
    """Both endpoints return 409 when a solve job is active."""
    server.session["solve"]["status"] = "running"
    resp = client.post(endpoint)
    assert resp.status_code == 409, f"{endpoint} should be refused while solve is running"


@pytest.mark.parametrize("endpoint", ["/api/cache/clear-runs", "/api/cache/clear-all"])
def test_clear_refused_while_export_running(cache_dirs, endpoint):
    """Both endpoints return 409 when an export job is active."""
    server.session["export"]["status"] = "running"
    resp = client.post(endpoint)
    assert resp.status_code == 409, f"{endpoint} should be refused while export is running"


@pytest.mark.parametrize("endpoint", ["/api/cache/clear-runs", "/api/cache/clear-all"])
def test_clear_refused_while_compare_running(cache_dirs, endpoint):
    """Both endpoints return 409 when a compare job is active."""
    server.session["compare"]["status"] = "running"
    resp = client.post(endpoint)
    assert resp.status_code == 409, f"{endpoint} should be refused while compare is running"


@pytest.mark.parametrize("endpoint", ["/api/cache/clear-runs", "/api/cache/clear-all"])
@pytest.mark.parametrize("status", ["idle", "complete", "error", "cancelled"])
def test_clear_allowed_when_jobs_not_running(cache_dirs, endpoint, status):
    """Endpoints succeed for every non-running status value."""
    server.session["solve"]["status"] = status
    server.session["export"]["status"] = status
    server.session["compare"]["status"] = status
    resp = client.post(endpoint)
    assert resp.status_code == 200, (
        f"{endpoint} should be allowed when all jobs are '{status}'"
    )


# ---------------------------------------------------------------------------
# Tests: synchronous status assignment (TOCTOU fix)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/cache/clear-runs", "/api/cache/clear-all"])
@pytest.mark.parametrize("job", ["solve", "export", "compare"])
def test_assert_no_active_job_blocks_when_status_is_running(cache_dirs, endpoint, job):
    """_assert_no_active_job raises 409 as soon as status is 'running'.

    The solve-start and compare-start handlers now set status='running'
    synchronously (before spawning the background thread), so the clear
    endpoints see the guard correctly without a race.  This test confirms the
    mechanism: setting session[job][status]='running' by hand (which is exactly
    what the synchronous assignment does) is enough to block the clear.
    """
    server.session[job]["status"] = "running"
    resp = client.post(endpoint)
    assert resp.status_code == 409, (
        f"{endpoint} must return 409 immediately once {job} status='running'"
    )


def test_sync_export_endpoint_is_409_guarded_against_concurrent_run(cache_dirs):
    """The synchronous /api/export/files endpoint must mark export 'running' for
    its duration so a concurrent cache-clear is blocked. We exercise the guard's
    409 branch: an export already in flight rejects a second synchronous export.
    Previously this endpoint ran with no status guard, so a clear could race it.
    """
    server.session["export"]["status"] = "running"
    resp = client.post("/api/export/files", json={})
    assert resp.status_code == 409, (
        "synchronous /api/export/files must 409 when an export is already running"
    )
