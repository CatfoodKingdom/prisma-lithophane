"""Real-dispatch tests for /api/palette/suggest.

These go through FastAPI payload parsing, _load_run_source_image, solve-config
construction, the shared target-cloud signature path, and response formatting —
only the expensive search backend is faked.  Added per the 2026-06-12 palette
domain-alignment review (the API boundary previously had zero test coverage,
which let silently-ignored removed payload fields survive).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
for _p in (_GEN_DIR, _ROOT / "Prisma"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import palette.suggest as palette_suggest_module  # noqa: E402
from palette.suggest import PaletteCandidate, PaletteSuggestionResult  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    from PIL import Image

    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, size=(8, 8, 3), dtype=np.uint8)
    Image.fromarray(img).save(str(tmp_path / "tiny.png"))
    monkeypatch.setattr(server, "_IMAGES_DIR", tmp_path)

    # Fake only the expensive search; everything upstream (payload parsing,
    # image loading, solve-config, shared signature path) runs for real.
    def _fake_suggest_palettes(sig, *, n_filaments, top_k, **kwargs):
        return PaletteSuggestionResult(candidates=[
            PaletteCandidate(
                filament_ids=[f"unit-{idx}" for idx in range(n_filaments)],
                mean_de=0.0123,
                max_de=0.05,
                pct_above_threshold=10.0,
                gamut_points=100,
                p90_de=0.02,
                rank_score=0.015,
                rank_mode="robust",
            )
        ], model_metadata={"estimated_with_three_color_rescore": True})

    monkeypatch.setattr(palette_suggest_module, "suggest_palettes_detailed", _fake_suggest_palettes)

    # Stub the gamut backend build (provider=None routes the white-rescale
    # provider through the cheap create path).
    monkeypatch.setattr(
        server,
        "_build_palette_suggestion_model",
        lambda snapshot: (SimpleNamespace(provider=None, domain="model_oklab"), {"model": "stub"}, {}),
    )
    return TestClient(server.app)


def _poll_suggest_result(client: TestClient, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = client.get("/api/palette/suggest/status").json()
        if status["status"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("suggest did not finish within timeout")


def test_suggest_dispatch_response_schema_is_honest(client: TestClient) -> None:
    resp = client.post("/api/palette/suggest", json={"image_path": "tiny.png", "top_k": 1})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, str) and job_id
    status = _poll_suggest_result(client)
    assert status["status"] == "complete", status
    assert status["job_id"] == job_id
    assert status["cancel_requested"] is False

    result = status["result"]
    cand = result["candidates"][0]
    assert cand["suggestion_mean_de"] == pytest.approx(0.0123, abs=1e-6)
    assert "source_rms_de" not in cand

    meta = result["model_metadata"]
    assert "nearest_color_space" not in meta
    assert meta["signature_domain"] == "model_oklab"
    assert isinstance(meta["model_domain_ingress"], bool)
    assert "model_domain_ingress_lut_path" in meta
    assert "metric_luminance_weight" in meta


def test_suggest_progress_is_monotonic_and_reaches_100_only_at_completion(
    client: TestClient,
    monkeypatch,
) -> None:
    progress_values = []
    original_update = server._update_suggest_job

    def recording_update(job_id, **updates):
        progress = updates.get("progress")
        if isinstance(progress, dict) and progress.get("stage_pct") is not None:
            progress_values.append(float(progress["stage_pct"]))
        return original_update(job_id, **updates)

    def fake_suggest(sig, *, n_filaments, top_k, progress, **kwargs):
        progress("search start", 0.0)
        progress("search late", 0.8)
        progress("stale child update", 0.3)
        progress("search complete", 1.0)
        return PaletteSuggestionResult(candidates=[
            PaletteCandidate(
                filament_ids=[f"unit-{idx}" for idx in range(n_filaments)],
                mean_de=0.0123,
                max_de=0.05,
                pct_above_threshold=10.0,
                gamut_points=100,
                p90_de=0.02,
                rank_score=0.015,
                rank_mode="robust",
            )
        ], model_metadata={"estimated_with_three_color_rescore": True})

    monkeypatch.setattr(server, "_update_suggest_job", recording_update)
    monkeypatch.setattr(palette_suggest_module, "suggest_palettes_detailed", fake_suggest)

    started = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "top_k": 1},
    )
    assert started.status_code == 200, started.text
    terminal = _poll_suggest_result(client)

    assert terminal["status"] == "complete"
    assert progress_values == sorted(progress_values)
    assert progress_values and max(progress_values) <= 99.0
    assert terminal["progress_detail"] == {
        "stage_label": "Complete",
        "stage_pct": 100,
    }


def test_suggest_immediate_cancel_survives_delayed_worker_entry(
    client: TestClient,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class DeferredThread:
        def __init__(self, *, target, daemon) -> None:  # type: ignore[no-untyped-def]
            captured["target"] = target
            captured["daemon"] = daemon

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=DeferredThread))
    monkeypatch.setattr(
        server,
        "_load_profile_sandbox",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled worker must stop before profile loading")
        ),
    )

    started = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "top_k": 1},
    )
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    assert captured["started"] is True

    stale = client.post("/api/palette/suggest/cancel?job_id=stale-job")
    assert stale.status_code == 409, stale.text

    first = client.post(f"/api/palette/suggest/cancel?job_id={job_id}")
    repeated = client.post(f"/api/palette/suggest/cancel?job_id={job_id}")
    assert first.status_code == 200, first.text
    assert first.json() == {
        "cancelled": True,
        "cancel_requested": True,
        "job_id": job_id,
    }
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["cancelled"] is True

    status = client.get("/api/palette/suggest/status").json()
    assert status["status"] == "running"
    assert status["cancel_requested"] is True
    assert status["job_id"] == job_id

    target = captured["target"]
    assert callable(target)
    target()

    terminal = client.get("/api/palette/suggest/status").json()
    assert terminal["status"] == "cancelled"
    assert terminal["cancel_requested"] is True
    assert terminal["job_id"] == job_id


def test_suggest_completion_decision_is_atomic_with_cancellation(monkeypatch) -> None:
    monkeypatch.setitem(
        server.session,
        "suggest",
        {
            "status": "running",
            "progress": {"stage_label": "Finishing"},
            "elapsed_s": 1.0,
            "result": None,
            "cancel_requested": True,
            "job_id": "job-cancelled-at-publication",
        },
    )

    completed = server._complete_suggest_job(  # type: ignore[attr-defined]
        "job-cancelled-at-publication",
        result={"candidates": ["partial"]},
        elapsed_s=2.0,
    )

    assert completed is False
    assert server.session["suggest"]["status"] == "running"
    assert server.session["suggest"]["result"] == {"candidates": ["partial"]}

    server.session["suggest"]["cancel_requested"] = False
    completed = server._complete_suggest_job(  # type: ignore[attr-defined]
        "job-cancelled-at-publication",
        result={"candidates": ["complete"]},
        elapsed_s=3.0,
    )

    assert completed is True
    assert server.session["suggest"]["status"] == "complete"
    assert server.session["suggest"]["result"] == {"candidates": ["complete"]}
    assert server.session["suggest"]["progress"] == {
        "stage_label": "Complete",
        "stage_pct": 100,
    }


def test_suggest_thread_start_failure_does_not_leave_running_job(
    client: TestClient,
    monkeypatch,
) -> None:
    class FailedThread:
        def __init__(self, *, target, daemon) -> None:  # type: ignore[no-untyped-def]
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            raise RuntimeError("simulated start failure")

    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=FailedThread))

    response = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "top_k": 1},
    )

    assert response.status_code == 500, response.text
    status = client.get("/api/palette/suggest/status").json()
    assert status["status"] == "error"
    assert "simulated start failure" in status["progress"]


def test_suggest_exact_path_returns_only_requested_size_and_count(
    client: TestClient,
    monkeypatch,
) -> None:
    captured = {}

    def fake_exact(sig, *, n_filaments, top_k, **kwargs):
        captured.update({"n_filaments": n_filaments, "top_k": top_k})
        candidates = [
            PaletteCandidate(
                filament_ids=[f"unit-{candidate_idx}-{color_idx}" for color_idx in range(n_filaments)],
                mean_de=0.01 + candidate_idx * 0.001,
                max_de=0.04,
                pct_above_threshold=8.0,
                gamut_points=n_filaments,
            )
            for candidate_idx in range(top_k)
        ]
        return PaletteSuggestionResult(
            candidates=candidates,
            model_metadata={
                "estimated_with_three_color_rescore": True,
                "three_color_rescore_finalists": top_k,
            },
        )

    monkeypatch.setattr(palette_suggest_module, "suggest_palettes_detailed", fake_exact)

    resp = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "n_filaments": 5, "top_k": 2},
    )

    assert resp.status_code == 200, resp.text
    status = _poll_suggest_result(client)
    assert status["status"] == "complete", status
    assert captured == {"n_filaments": 5, "top_k": 2}
    result = status["result"]
    assert len(result["candidates"]) == 2
    assert all(len(candidate["filament_ids"]) == 5 for candidate in result["candidates"])
    assert "tiers" not in result
    assert "alternatives" not in result
    assert "recommended" not in result
    assert "per_load_capped" not in result
    assert result["model_metadata"]["three_color_rescore_finalists"] == 2


def test_suggest_exact_size_is_not_clamped_to_printer_capacity(
    client: TestClient,
    monkeypatch,
) -> None:
    captured = {}

    def fake_exact(sig, *, n_filaments, top_k, **kwargs):
        captured["n_filaments"] = n_filaments
        return PaletteSuggestionResult(
            candidates=[
                PaletteCandidate(
                    filament_ids=[f"unit-{idx}" for idx in range(n_filaments)],
                    mean_de=0.01,
                    max_de=0.04,
                    pct_above_threshold=8.0,
                    gamut_points=n_filaments,
                )
            ],
            model_metadata={},
        )

    monkeypatch.setattr(palette_suggest_module, "suggest_palettes_detailed", fake_exact)
    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {"printer": {"ams_units": 1, "slots_per_ams": 4}, "nozzle": None},
    )

    resp = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "n_filaments": 8, "top_k": 1},
    )
    assert resp.status_code == 200, resp.text
    status = _poll_suggest_result(client)
    assert status["status"] == "complete", status
    assert captured["n_filaments"] == 8
    assert len(status["result"]["candidates"][0]["filament_ids"]) == 8


def test_suggest_default_pool_excludes_reserved_base_and_cap(
    client: TestClient,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_exact(sig, *, n_filaments, top_k, exclude_filament_ids, **kwargs):
        captured["excluded"] = set(exclude_filament_ids)
        return PaletteSuggestionResult(candidates=[], model_metadata={})

    monkeypatch.setattr(palette_suggest_module, "suggest_palettes_detailed", fake_exact)
    monkeypatch.setattr(server, "_white_ids", lambda _cfg=None: ["base-white", "cap-white"])

    resp = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "n_filaments": 3, "top_k": 1},
    )
    assert resp.status_code == 200, resp.text
    status = _poll_suggest_result(client)
    assert status["status"] == "complete", status
    assert {"base-white", "cap-white"}.issubset(captured["excluded"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n_filaments", 1),
        ("n_filaments", 17),
        ("top_k", 0),
        ("top_k", 11),
    ],
)
def test_suggest_exact_numeric_bounds_are_enforced(
    client: TestClient,
    field: str,
    value: int,
) -> None:
    response = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", field: value},
    )
    assert response.status_code == 422, response.text


def test_suggest_rejects_duplicate_explicit_candidate_ids(client: TestClient) -> None:
    response = client.post(
        "/api/palette/suggest",
        json={
            "image_path": "tiny.png",
            "n_filaments": 2,
            "filament_ids": ["unit-a", "unit-a"],
        },
    )
    assert response.status_code == 422, response.text
    assert "must not contain duplicates" in response.text


def test_suggest_rejects_reserved_base_as_color_candidate(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_cfg",
        lambda: {"white_base": "reserved-white", "d_wb": 0.2},
    )
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    response = client.post(
        "/api/palette/suggest",
        json={
            "image_path": "tiny.png",
            "n_filaments": 2,
            "filament_ids": ["reserved-white", "unit-a"],
        },
    )
    assert response.status_code == 400, response.text
    assert "Reserved base/cap filaments" in response.text


def test_suggest_rejects_too_few_selected_eligible_colors(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_load_registry",
        lambda: {"excluded": {"exclude_from_model": True}},
    )
    response = client.post(
        "/api/palette/suggest",
        json={
            "image_path": "tiny.png",
            "n_filaments": 3,
            "filament_ids": ["unit-a", "unit-b", "excluded"],
        },
    )
    assert response.status_code == 400, response.text
    assert "only 2 selected eligible color filaments" in response.text


@pytest.mark.parametrize(
    "stale_field",
    [
        {"search_mode": "quality"},
        {"quality_weights": {"mean_de": 1.0}},
        {"max_swaps": 1},
        {"improvement_threshold": 2.0},
        {"force_all_tiers": True},
    ],
)
def test_removed_suggestion_fields_are_rejected_loudly(client: TestClient, stale_field: dict) -> None:
    payload = {"image_path": "tiny.png", **stale_field}
    resp = client.post("/api/palette/suggest", json=payload)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(list(stale_field)[0] in str(item.get("loc", "")) for item in detail)


def test_unknown_fields_rejected_generally(client: TestClient) -> None:
    resp = client.post(
        "/api/palette/suggest",
        json={"image_path": "tiny.png", "definitely_not_a_field": 1},
    )
    assert resp.status_code == 422


def test_suggest_refuses_excluded_white_base(client, monkeypatch):
    # A configured white base/cap flagged exclude_from_model must be refused up
    # front (it is a fixed input, not a droppable color candidate).
    monkeypatch.setattr(server, "_cfg", lambda: {"white_base": "excluded-white", "d_wb": 0.2})
    monkeypatch.setattr(server, "_load_registry",
                        lambda: {"excluded-white": {"exclude_from_model": True}})
    resp = client.post("/api/palette/suggest", json={"image_path": "tiny.png"})
    assert resp.status_code == 400
    assert "excluded-white" in resp.text
