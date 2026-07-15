from __future__ import annotations

import inspect
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import maintenance
import maintenance_reextract
import processing.artifact_sinks as artifact_sinks
import processing.extraction as extraction
import server
import sqlite_data_access
from sqlite_data_access import SQLiteDataStore
from tests.calibration.test_backend_selector import (
    _materialize_stage2c_fixture_assets,
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)
from tests.calibration.test_sqlite_stage4_extraction_writes import _result as _extraction_result
from tests.calibration.test_sqlite_stage3b_image_custody import _add_image_asset


def _store(tmp_path: Path) -> SQLiteDataStore:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _install_store(store: SQLiteDataStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server, "_store", store)
    with server._maintenance_jobs_lock:
        server._maintenance_jobs.clear()
    with server._maintenance_preflights_lock:
        server._maintenance_preflights.clear()
    with server._maintenance_resource_gate_lock:
        server._ordinary_resource_leases.clear()


def _install_queued_maintenance_job(operation_id: str) -> str:
    job_id = f"queued-{operation_id}"
    with server._maintenance_jobs_lock:
        server._maintenance_jobs[job_id] = {
            "job_id": job_id,
            "operation_id": operation_id,
            "status": "queued",
        }
    return job_id


def _wait_for_maintenance_job(client: TestClient, job_id: str) -> dict:
    status_payload: dict | None = None
    for _ in range(400):
        status = client.get(f"/api/maintenance/jobs/{job_id}")
        assert status.status_code == 200, status.text
        status_payload = status.json()
        if status_payload["status"] in {"succeeded", "failed", "cancelled"}:
            return status_payload
        time.sleep(0.05)
    raise AssertionError(f"maintenance job did not finish: {status_payload}")


def _tree_file_snapshot(*roots: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            snapshot[f"{root.name}/{path.relative_to(root).as_posix()}"] = path.read_bytes()
    return snapshot


def test_maintenance_operations_preflight_and_sqlite_job_write_report(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    operations_response = client.get("/api/maintenance/operations")
    assert operations_response.status_code == 200, operations_response.text
    operations = operations_response.json()["operations"]
    operation_ids = {operation["operation_id"] for operation in operations}
    assert "sqlite_integrity_check" in operation_ids
    assert "sqlite_vacuum_optimize" not in operation_ids
    assert "rebuild_extraction_visuals" in operation_ids
    assert "export_geometry_files" in operation_ids
    assert "recompute_appearance_data" not in operation_ids
    assert "reextract_sample_images" in operation_ids
    assert "sample_reextraction" not in operation_ids
    rebuild_visuals = next(op for op in operations if op["operation_id"] == "rebuild_extraction_visuals")
    assert rebuild_visuals["description"] == (
        "Rebuild durable source.jpg and strip.jpg images from accepted processed samples."
    )
    assert rebuild_visuals["conflict_resources"] == [
        "image_custody",
        "extraction_evidence",
        "sample_visuals",
    ]
    reextract_operation = next(op for op in operations if op["operation_id"] == "reextract_sample_images")
    assert reextract_operation["enabled"] is True
    assert reextract_operation["name"] == "Re-extract Sample Images"
    assert reextract_operation["category"] == "Images"
    cancellation_policies = {
        operation["operation_id"]: operation["cancellation_policy"]
        for operation in operations
    }
    assert cancellation_policies == {
        "audit_library_integrity": "not_supported",
        "audit_missing_artifacts": "safe_points",
        "audit_orphaned_artifacts": "safe_points",
        "quarantine_orphaned_artifacts": "safe_points",
        "audit_source_image_custody": "safe_points",
        "rebuild_image_previews": "safe_points",
        "rebuild_extraction_visuals": "safe_points",
        "sqlite_integrity_check": "not_supported",
        "regenerate_managed_step_artifacts": "safe_points",
        "export_geometry_files": "safe_points",
        "refit_calibration_models": "not_supported",
        "reextract_sample_images": "safe_points",
    }
    assert {
        operation["operation_id"]
        for operation in operations
        if operation["cancellable"]
    } == {
        operation_id
        for operation_id, policy in cancellation_policies.items()
        if policy == "safe_points"
    }
    for operation in maintenance.OPERATIONS.values():
        if operation.cancellable and operation.execute is not None:
            executor_source = inspect.getsource(operation.execute)
            assert "_cancelled(should_cancel)" in executor_source, operation.operation_id

    removed_preflight = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "sqlite_vacuum_optimize"},
    )
    assert removed_preflight.status_code == 404, removed_preflight.text
    removed_job = client.post(
        "/api/maintenance/jobs",
        json={"operation_id": "sqlite_vacuum_optimize"},
    )
    assert removed_job.status_code == 404, removed_job.text

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "sqlite_integrity_check"},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight_payload = preflight_response.json()
    assert preflight_payload["preflight"]["ui_refresh"]["kind"] == "none"

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "sqlite_integrity_check",
            "preflight_token": preflight_payload["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    assert job_payload["result"]["summary"]["ok"] is True
    assert job_payload["result"]["ui_refresh"]["kind"] == "none"
    assert job_payload["report_id"]

    reports_response = client.get("/api/maintenance/reports")
    assert reports_response.status_code == 200, reports_response.text
    assert any(report["report_id"] == job_payload["report_id"] for report in reports_response.json()["reports"])

    report_response = client.get(f"/api/maintenance/reports/{job_payload['report_id']}")
    assert report_response.status_code == 200, report_response.text
    assert report_response.json()["operation_id"] == "sqlite_integrity_check"


@pytest.mark.parametrize(
    ("operation_id", "method", "path", "payload"),
    [
        ("rebuild_image_previews", "post", "/api/images/example.cr2/rotation", {"rotation_cw": 1}),
        ("rebuild_image_previews", "post", "/api/blanks/register", {"filename": "example.cr2"}),
        ("rebuild_image_previews", "post", "/api/images/cleanup-unused", None),
        ("rebuild_extraction_visuals", "post", "/api/samples/exp-001/reject", None),
        ("regenerate_managed_step_artifacts", "post", "/api/geometries/not-a-geometry/artifacts", {}),
        ("audit_library_integrity", "post", "/api/samples/exp-001/flag", {"reason": "test"}),
    ],
)
def test_queued_maintenance_reservation_blocks_conflicting_endpoint_writers(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
    operation_id: str,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    _install_queued_maintenance_job(operation_id)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.request(method, path, json=payload)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert operation_id in detail
    assert "conflicting resources" in detail


def test_preview_get_is_treated_as_writer_but_unrelated_read_and_write_remain_available(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    _install_queued_maintenance_job("rebuild_image_previews")
    client = TestClient(server.app, raise_server_exceptions=False)

    preview = client.get("/api/previews/example.cr2")
    samples = client.get("/api/samples")
    flag = client.post("/api/samples/exp-001/flag", json={"reason": "unrelated"})

    assert preview.status_code == 409, preview.text
    assert samples.status_code == 200, samples.text
    assert flag.status_code == 200, flag.text


def test_ordinary_writer_lease_blocks_maintenance_admission_and_releases_cleanly(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    lease_id, blocker = server._try_acquire_ordinary_resource_lease(  # type: ignore[attr-defined]
        {"preview_cache"},
        owner="test preview publication",
    )
    assert lease_id is not None
    assert blocker is None
    try:
        blocked = client.post(
            "/api/maintenance/jobs",
            json={"operation_id": "rebuild_image_previews", "mode": "missing_only"},
        )
        assert blocked.status_code == 409, blocked.text
        assert "test preview publication" in blocked.json()["detail"]
    finally:
        server._release_ordinary_resource_lease(lease_id)  # type: ignore[attr-defined]

    assert server._ordinary_resource_leases == {}  # type: ignore[attr-defined]


def test_queued_maintenance_reservation_keeps_reads_and_cancellation_available(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    job_id = _install_queued_maintenance_job("rebuild_image_previews")
    client = TestClient(server.app, raise_server_exceptions=False)

    assert client.get("/api/samples").status_code == 200
    queued = client.get(f"/api/maintenance/jobs/{job_id}")
    assert queued.status_code == 200, queued.text
    assert queued.json()["cancellable"] is True
    assert queued.json()["cancel_available"] is True
    cancel = client.post(f"/api/maintenance/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    payload = cancel.json()
    assert payload["status"] == "cancelling"
    assert payload["cancel_requested"] is True
    assert payload["cancellable"] is True
    assert payload["cancel_available"] is False
    assert payload["progress"]["phase"] == "cancelling"
    assert payload["progress"]["message"] == "Cancelling after current safe point"

    server._maintenance_progress_callback(job_id)(  # type: ignore[attr-defined]
        phase="work",
        message="Worker is still finishing an item",
        current=3,
        total=10,
    )
    after_progress = client.get(f"/api/maintenance/jobs/{job_id}")
    assert after_progress.status_code == 200, after_progress.text
    assert after_progress.json()["progress"]["phase"] == "cancelling"
    assert after_progress.json()["progress"]["message"] == "Cancelling after current safe point"
    assert after_progress.json()["progress"]["current"] == 3

    repeated = client.post(f"/api/maintenance/jobs/{job_id}/cancel")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "cancelling"
    assert client.get("/api/previews/example.cr2").status_code == 409


@pytest.mark.parametrize(
    "operation_id",
    ["audit_library_integrity", "sqlite_integrity_check", "refit_calibration_models"],
)
def test_non_cancellable_maintenance_operations_reject_cancel(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
    operation_id: str,
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    job_id = _install_queued_maintenance_job(operation_id)
    client = TestClient(server.app, raise_server_exceptions=False)

    cancel = client.post(f"/api/maintenance/jobs/{job_id}/cancel")

    assert cancel.status_code == 409, cancel.text
    assert "runs to completion" in cancel.json()["detail"]
    with server._maintenance_jobs_lock:
        job = dict(server._maintenance_jobs[job_id])
    assert job["status"] == "queued"
    assert job.get("cancel_requested") is not True


def test_terminal_maintenance_job_rejects_late_cancel(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    job_id = _install_queued_maintenance_job("rebuild_image_previews")
    with server._maintenance_jobs_lock:
        server._maintenance_jobs[job_id]["status"] = "succeeded"
    client = TestClient(server.app, raise_server_exceptions=False)

    cancel = client.post(f"/api/maintenance/jobs/{job_id}/cancel")

    assert cancel.status_code == 409, cancel.text
    assert "already succeeded" in cancel.json()["detail"]


def test_maintenance_cancel_transitions_through_cancelling_to_cancelled(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    started = threading.Event()

    def cancellable_executor(
        _store,
        mode,
        scope,
        *,
        progress_cb,
        should_cancel,
    ) -> dict:
        started.set()
        if progress_cb:
            progress_cb(
                phase="work",
                message="Waiting at cancellable test barrier",
                current=0,
                total=10,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                return maintenance._report(  # type: ignore[attr-defined]
                    operation_id="rebuild_image_previews",
                    mode=mode,
                    scope=scope,
                    status="cancelled",
                    started_at=maintenance._now_iso(),  # type: ignore[attr-defined]
                    summary={"cancelled": 1},
                    ui_refresh=maintenance.ui_refresh_none("Cancelled for test."),
                )
            time.sleep(0.005)
        raise AssertionError("test executor did not observe cancellation")

    original = maintenance.OPERATIONS["rebuild_image_previews"]
    monkeypatch.setitem(
        maintenance.OPERATIONS,
        "rebuild_image_previews",
        replace(original, execute=cancellable_executor),
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    start = client.post(
        "/api/maintenance/jobs",
        json={"operation_id": "rebuild_image_previews", "mode": "missing_only"},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    assert started.wait(timeout=2)

    cancel = client.post(f"/api/maintenance/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelling"
    assert cancel.json()["cancel_available"] is False
    assert client.post(f"/api/maintenance/jobs/{job_id}/cancel").status_code == 200

    terminal = _wait_for_maintenance_job(client, job_id)
    assert terminal["status"] == "cancelled"
    assert terminal["result"]["status"] == "cancelled"
    assert terminal["error"] is None
    assert terminal["cancel_available"] is False
    assert terminal["progress"]["phase"] == "cancelled"
    assert terminal["progress"]["message"] == "Maintenance job cancelled"
    assert terminal["progress"]["percent"] < 100
    assert server._find_running_maintenance_job() is None  # type: ignore[attr-defined]
    assert server._maintenance_resource_blocker(frozenset({"preview_cache"})) is None  # type: ignore[attr-defined]


def test_maintenance_cancel_before_worker_start_remains_cancelling_until_terminal(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    preflight = maintenance.preflight_operation(
        store,
        "rebuild_image_previews",
        mode="missing_only",
    )
    job = server._create_maintenance_job(  # type: ignore[attr-defined]
        operation_id="rebuild_image_previews",
        mode="missing_only",
        scope={},
        preflight=preflight,
    )
    job_id = job["job_id"]
    client = TestClient(server.app, raise_server_exceptions=False)

    cancel = client.post(f"/api/maintenance/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelling"

    server._run_maintenance_job(job_id)  # type: ignore[attr-defined]

    terminal = client.get(f"/api/maintenance/jobs/{job_id}")
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "cancelled"
    assert terminal.json()["result"]["status"] == "cancelled"
    assert terminal.json()["error"] is None
    assert terminal.json()["progress"]["phase"] == "cancelled"
    assert terminal.json()["progress"]["message"] == "Maintenance job cancelled"
    assert terminal.json()["progress"]["percent"] < 100


def test_maintenance_thread_start_failure_releases_queued_resource_reservation(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)

    def fail_start(_thread) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated thread start failure")

    monkeypatch.setattr(server.threading.Thread, "start", fail_start)
    with pytest.raises(server.HTTPException) as raised:
        server.start_maintenance_job_endpoint(
            {"operation_id": "rebuild_image_previews", "mode": "missing_only"}
        )

    assert raised.value.status_code == 500
    with server._maintenance_jobs_lock:
        failed_jobs = list(server._maintenance_jobs.values())
    assert len(failed_jobs) == 1
    assert failed_jobs[0]["status"] == "failed"
    assert server._maintenance_resource_blocker(frozenset({"preview_cache"})) is None  # type: ignore[attr-defined]


def test_maintenance_report_log_clear_deletes_only_direct_report_json(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    report_root = server._maintenance_reports_dir(store)  # type: ignore[attr-defined]
    report_root.mkdir(parents=True)
    report_a = report_root / "20260626_audit_one.json"
    report_b = report_root / "20260626_audit_two.json"
    report_a.write_text('{"operation_id": "audit_library_integrity"}', encoding="utf-8")
    report_b.write_text('{"operation_id": "sqlite_integrity_check"}', encoding="utf-8")
    keep_text = report_root / "keep.txt"
    keep_text.write_text("not a report", encoding="utf-8")
    nested_dir = report_root / "nested"
    nested_dir.mkdir()
    nested_report = nested_dir / "nested_report.json"
    nested_report.write_text("{}", encoding="utf-8")
    json_directory = report_root / "folder.json"
    json_directory.mkdir()
    quarantine_file = report_root.parent / "quarantine" / "keep_quarantine.json"
    quarantine_file.parent.mkdir(parents=True)
    quarantine_file.write_text("{}", encoding="utf-8")

    client = TestClient(server.app, raise_server_exceptions=False)
    clear_response = client.delete("/api/maintenance/reports")

    assert clear_response.status_code == 200, clear_response.text
    payload = clear_response.json()
    assert payload["deleted_count"] == 2
    assert payload["skipped_count"] == 1
    assert payload["failed_count"] == 0
    assert not report_a.exists()
    assert not report_b.exists()
    assert keep_text.exists()
    assert nested_report.exists()
    assert json_directory.exists()
    assert quarantine_file.exists()
    reports_response = client.get("/api/maintenance/reports")
    assert reports_response.status_code == 200, reports_response.text
    assert reports_response.json()["reports"] == []


def test_maintenance_report_log_clear_missing_directory_is_noop(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    clear_response = client.delete("/api/maintenance/reports")

    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json() == {
        "deleted_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "failures": [],
    }


def test_maintenance_missing_artifact_audit_requires_only_durable_sample_visuals(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    report = maintenance.execute_operation(store, "audit_missing_artifacts")

    missing = {
        item["artifact"]
        for item in report["findings"]
        if item.get("category") == "sample_thumbnail_missing"
        and item.get("sample_id") == "exp-001"
    }
    assert missing == {"source", "strip"}
    assert "mock" not in missing


def test_maintenance_registry_omits_mock_strip_rebuild(
    tmp_path: Path,
) -> None:
    _store(tmp_path)
    operations = {operation["operation_id"] for operation in maintenance.list_operations()}

    assert "rebuild_mock_sample_strips" not in operations


def test_maintenance_rebuild_extraction_visuals_uses_provenance_and_targeted_refresh(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    _install_store(store, monkeypatch)

    raw_load_calls: list[Path] = []

    def fake_load_raw_both(path):  # type: ignore[no-untyped-def]
        raw_load_calls.append(Path(path))
        return (
            np.tile(np.linspace(20, 220, 240, dtype=np.uint8), (80, 1)).reshape(80, 240, 1).repeat(3, axis=2),
            np.zeros((80, 240, 3), dtype=np.float32),
        )

    monkeypatch.setattr(maintenance, "load_raw_both", fake_load_raw_both)
    monkeypatch.setattr(
        maintenance,
        "load_preview_jpeg",
        lambda *_args, **_kwargs: np.full((80, 240, 3), 96, dtype=np.uint8),
    )
    monkeypatch.setattr(maintenance, "detect_swatch_extent", lambda _img, _cfg, **_kwargs: (0, 0, 20, 10))
    monkeypatch.setattr(maintenance, "find_swatch_boundaries", lambda *_args, **_kwargs: [0, 7, 14, 20])
    client = TestClient(server.app, raise_server_exceptions=False)

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "rebuild_extraction_visuals", "mode": "missing_only"},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["summary"]["targets"] == 2
    assert preflight["summary"]["blocked"] == 0
    assert preflight["summary"]["measurements_updated"] is False

    before_result = store.get_extraction_result("exp-001")
    before_sample = store.get_sample("exp-001").model_dump()
    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "rebuild_extraction_visuals",
            "mode": "missing_only",
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    result = job_payload["result"]
    assert result["summary"]["rebuilt_files"] == 2
    assert result["summary"]["overlay_policy"] == "accepted_source_boundary_and_extracted_strip"
    assert result["summary"]["measurements_updated"] is False
    assert (store.root / "thumbnails" / "exp-001" / "source.jpg").exists()
    assert (store.root / "thumbnails" / "exp-001" / "strip.jpg").exists()
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()
    assert not (store.root / "thumbnails" / "exp-001" / "blank.jpg").exists()
    assert len(raw_load_calls) == 1
    assert store.get_extraction_result("exp-001") == before_result
    assert store.get_sample("exp-001").model_dump() == before_sample
    refresh = result["ui_refresh"]
    assert refresh["kind"] == "targeted"
    assert refresh["invalidate_sample_thumbnails"]["sample_ids"] == ["exp-001"]
    assert refresh["invalidate_sample_thumbnails"]["kinds"] == ["source", "strip"]


def test_maintenance_rebuild_extraction_visuals_preserves_manual_orientation_without_display_flip(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    manual_accepted = accepted.model_copy(
        update={
            "method": "manual",
            "method_provenance": accepted.method_provenance.model_copy(
                update={
                    "strip_location_source": "manual_corner_selection",
                    "coordinate_space": "manual_full_image_after_source_rotation_before_open_side_rotation",
                }
            ),
        }
    )
    store.save_extraction_result("exp-001", manual_accepted.model_dump())
    sample = store.get_sample("exp-001")

    def forbidden_display_flip(_strip):  # type: ignore[no-untyped-def]
        raise AssertionError("manual extraction visual rebuild must not use brightness display flipping")

    monkeypatch.setattr(
        maintenance,
        "load_raw_both",
        lambda _path: (
            np.tile(np.linspace(20, 220, 240, dtype=np.uint8), (80, 1)).reshape(80, 240, 1).repeat(3, axis=2),
            np.zeros((80, 240, 3), dtype=np.float32),
        ),
    )
    monkeypatch.setattr(maintenance, "_strip_needs_display_flip", forbidden_display_flip)
    monkeypatch.setattr(maintenance, "detect_swatch_extent", lambda _img, _cfg, **_kwargs: (0, 0, 20, 10))
    monkeypatch.setattr(maintenance, "find_swatch_boundaries", lambda *_args, **_kwargs: [0, 7, 14, 20])

    strip_bgr, _inner_x, _inner_y, _inner_w, _inner_h, _boundaries, sampling_boxes = (
        maintenance._reconstructed_strip_and_sampling_boxes(  # type: ignore[attr-defined]
            store,
            sample,
            store.get_extraction_result("exp-001"),
        )
    )

    assert strip_bgr.size > 0
    assert sampling_boxes


def test_maintenance_rebuild_extraction_visuals_does_not_use_old_thumbnail_helpers(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())

    def prohibited(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("old thumbnail regeneration path must not be used")

    monkeypatch.setattr(server, "_ensure_sample_thumbnails", prohibited)
    monkeypatch.setattr(server, "_process_sample", prohibited)
    monkeypatch.setattr(
        maintenance,
        "load_raw_both",
        lambda _path: (np.full((80, 240, 3), 120, dtype=np.uint8), np.zeros((80, 240, 3), dtype=np.float32)),
    )
    monkeypatch.setattr(maintenance, "load_preview_jpeg", lambda *_args, **_kwargs: np.full((80, 240, 3), 96, dtype=np.uint8))
    monkeypatch.setattr(maintenance, "detect_swatch_extent", lambda _img, _cfg, **_kwargs: (0, 0, 20, 10))
    monkeypatch.setattr(maintenance, "find_swatch_boundaries", lambda *_args, **_kwargs: [0, 7, 14, 20])
    report = maintenance.execute_operation(store, "rebuild_extraction_visuals", mode="missing_only")

    assert report["status"] == "completed"
    assert report["summary"]["rebuilt_files"] == 2


def test_maintenance_rebuild_preserves_sample_visual_pair_when_one_renderer_fails(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())

    live_dir = store.root / "thumbnails" / "exp-001"
    live_dir.mkdir(parents=True)
    live_source = live_dir / "source.jpg"
    live_strip = live_dir / "strip.jpg"
    live_source.write_bytes(b"old source")
    live_strip.write_bytes(b"old strip")

    def write_source(_store, _sample, _result, path, **_kwargs):  # type: ignore[no-untyped-def]
        path.write_bytes(b"new source")
        return np.full((10, 20, 3), 100, dtype=np.uint8)

    def fail_strip(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("strip renderer failed")

    monkeypatch.setattr(maintenance, "_rebuild_extraction_source_visual", write_source)
    monkeypatch.setattr(maintenance, "_rebuild_extraction_strip_visual", fail_strip)

    report = maintenance.execute_operation(store, "rebuild_extraction_visuals", mode="force")

    assert report["status"] == "failed"
    assert report["summary"]["rebuilt_files"] == 0
    assert report["summary"]["errors"] == 1
    assert report["summary"]["partial_success"] is False
    assert "strip renderer failed" in report["errors"][0]
    assert live_source.read_bytes() == b"old source"
    assert live_strip.read_bytes() == b"old strip"
    assert not list(live_dir.glob(".*.stage.*.jpg"))


def test_maintenance_rebuild_cancellation_preserves_sample_visual_pair(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    live_dir = store.root / "thumbnails" / "exp-001"
    live_dir.mkdir(parents=True)
    live_source = live_dir / "source.jpg"
    live_strip = live_dir / "strip.jpg"
    live_source.write_bytes(b"old source")
    live_strip.write_bytes(b"old strip")
    cancelled = False

    def write_source(_store, _sample, _result, path, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal cancelled
        path.write_bytes(b"new source")
        cancelled = True
        return np.full((10, 20, 3), 100, dtype=np.uint8)

    monkeypatch.setattr(maintenance, "_rebuild_extraction_source_visual", write_source)

    report = maintenance._execute_rebuild_extraction_visuals(  # type: ignore[attr-defined]
        store,
        "force",
        {},
        progress_cb=None,
        should_cancel=lambda: cancelled,
    )

    assert report["status"] == "cancelled"
    assert report["summary"]["rebuilt_files"] == 0
    assert live_source.read_bytes() == b"old source"
    assert live_strip.read_bytes() == b"old strip"
    assert not list(live_dir.glob(".*.stage.*.jpg"))


def test_preview_pair_encoder_failure_preserves_existing_pair(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    out_dir = tmp_path / "previews"
    out_dir.mkdir()
    full = out_dir / "cache.jpg"
    small = out_dir / "cache_small.jpg"
    full.write_bytes(b"old full")
    small.write_bytes(b"old small")
    monkeypatch.setattr(
        extraction,
        "load_preview_jpeg",
        lambda *_args, **_kwargs: np.full((40, 80, 3), 120, dtype=np.uint8),
    )
    real_imwrite = artifact_sinks.cv2.imwrite
    calls = 0

    def fail_second_encode(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return real_imwrite(*args, **kwargs)

    monkeypatch.setattr(artifact_sinks.cv2, "imwrite", fail_second_encode)

    with pytest.raises(RuntimeError, match="JPEG encoder"):
        extraction.generate_preview_jpeg(tmp_path / "source.tif", out_dir, cache_stem="cache")

    assert full.read_bytes() == b"old full"
    assert small.read_bytes() == b"old small"
    assert not list(out_dir.glob(".*.stage.*.jpg"))


def test_preview_decode_failure_preserves_existing_pair(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    out_dir = tmp_path / "previews"
    out_dir.mkdir()
    full = out_dir / "cache.jpg"
    small = out_dir / "cache_small.jpg"
    full.write_bytes(b"old full")
    small.write_bytes(b"old small")
    monkeypatch.setattr(extraction, "load_preview_jpeg", lambda *_args, **_kwargs: None)

    result = extraction.generate_preview_jpeg(
        tmp_path / "unreadable-source.tif",
        out_dir,
        cache_stem="cache",
    )

    assert result is None
    assert full.read_bytes() == b"old full"
    assert small.read_bytes() == b"old small"


def test_force_preview_rebuild_does_not_delete_current_pair_before_generation(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    cache_stem = maintenance.preview_cache_stem(store, "sample.CR2")
    full, small = maintenance.preview_pair_paths(store, cache_stem)
    full.parent.mkdir(parents=True)
    full.write_bytes(b"old full")
    small.write_bytes(b"old small")
    monkeypatch.setattr(
        maintenance,
        "generate_preview_jpeg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected generation failure")),
    )

    report = maintenance.execute_operation(store, "rebuild_image_previews", mode="force")

    assert report["status"] == "failed"
    assert full.read_bytes() == b"old full"
    assert small.read_bytes() == b"old small"


def test_preview_pair_promotion_failure_rolls_back_both_files(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    out_dir = tmp_path / "previews"
    out_dir.mkdir()
    full = out_dir / "cache.jpg"
    small = out_dir / "cache_small.jpg"
    full.write_bytes(b"old full")
    small.write_bytes(b"old small")
    monkeypatch.setattr(
        extraction,
        "load_preview_jpeg",
        lambda *_args, **_kwargs: np.full((40, 80, 3), 120, dtype=np.uint8),
    )
    real_replace = artifact_sinks.os.replace
    promotion_calls = 0

    def fail_second_promotion(src, dst):  # type: ignore[no-untyped-def]
        nonlocal promotion_calls
        if ".stage." in Path(src).name:
            promotion_calls += 1
            if promotion_calls == 2:
                raise OSError("injected second promotion failure")
        return real_replace(src, dst)

    monkeypatch.setattr(artifact_sinks.os, "replace", fail_second_promotion)

    with pytest.raises(RuntimeError, match="artifact publication failed"):
        extraction.generate_preview_jpeg(tmp_path / "source.tif", out_dir, cache_stem="cache")

    assert full.read_bytes() == b"old full"
    assert small.read_bytes() == b"old small"
    assert not list(out_dir.glob(".*.stage.*.jpg"))
    assert not list(out_dir.glob(".*.rollback.*.jpg"))


def test_preview_pair_preserves_recovery_copy_when_rollback_itself_fails(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    out_dir = tmp_path / "previews"
    out_dir.mkdir()
    full = out_dir / "cache.jpg"
    small = out_dir / "cache_small.jpg"
    full.write_bytes(b"old full")
    small.write_bytes(b"old small")
    monkeypatch.setattr(
        extraction,
        "load_preview_jpeg",
        lambda *_args, **_kwargs: np.full((40, 80, 3), 120, dtype=np.uint8),
    )
    real_replace = artifact_sinks.os.replace
    promotion_calls = 0

    def fail_promotion_and_rollback(src, dst):  # type: ignore[no-untyped-def]
        nonlocal promotion_calls
        source_name = Path(src).name
        if ".stage." in source_name:
            promotion_calls += 1
            if promotion_calls == 2:
                raise OSError("injected promotion failure")
        if ".rollback." in source_name:
            raise OSError("injected rollback failure")
        return real_replace(src, dst)

    monkeypatch.setattr(artifact_sinks.os, "replace", fail_promotion_and_rollback)

    with pytest.raises(RuntimeError, match="recovery copy preserved at"):
        extraction.generate_preview_jpeg(tmp_path / "source.tif", out_dir, cache_stem="cache")

    recovery_copies = list(out_dir.glob(".*.rollback.*.jpg"))
    assert len(recovery_copies) == 1
    assert recovery_copies[0].read_bytes() == b"old full"
    assert small.read_bytes() == b"old small"


def test_thumbnail_encoder_false_return_preserves_existing_file(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    target = tmp_path / "source.jpg"
    target.write_bytes(b"old thumbnail")
    monkeypatch.setattr(artifact_sinks.cv2, "imwrite", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="JPEG encoder"):
        artifact_sinks.write_thumbnail_image(
            np.full((20, 40, 3), 100, dtype=np.uint8),
            target,
        )

    assert target.read_bytes() == b"old thumbnail"
    assert not list(tmp_path.glob(".*.stage.*.jpg"))


def test_maintenance_reextract_appearance_only_updates_sidecar_and_targets_refresh(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _materialize_stage2c_fixture_assets(store.root)
    accepted = _extraction_result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-new", "included_swatch_count": 2}]
    store.publish_model_fit(model_kind="camera_transform", model_fit_id="fit-ct", contributors=contributor)
    store.publish_model_fit(model_kind="photo_stack_v2", model_fit_id="fit-photo", contributors=contributor)
    _install_store(store, monkeypatch)

    monkeypatch.setattr(
        maintenance_reextract,
        "_embedded_jpeg_extraction",
        lambda **_kwargs: SimpleNamespace(
            colors_by_swatch_index={
                0: np.array([10.0, 20.0, 30.0]),
                1: np.array([40.0, 50.0, 60.0]),
            },
            appearance_source=maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
            flipped=False,
            order_correlation=0.99,
            strip_rgb=np.full((10, 24, 3), 180, dtype=np.uint8),
            boxes_by_swatch_index={0: (2, 2, 8, 8), 1: (10, 2, 16, 8)},
        ),
    )
    monkeypatch.setattr(
        maintenance_reextract,
        "appearance_strip_visual_from_extraction",
        lambda _extraction: np.full((10, 24, 3), 180, dtype=np.uint8),
    )
    monkeypatch.setattr(
        maintenance_reextract,
        "_source_strip_and_sampling_boxes_from_target",
        lambda *_args, **_kwargs: (
            np.full((10, 24, 3), 120, dtype=np.uint8),
            {0: (2, 2, 8, 8), 1: (10, 2, 16, 8)},
            {"coordinate_space": "test"},
        ),
    )
    monkeypatch.setattr(maintenance_reextract, "_decode_environment", lambda: {"rawpy": "new"})
    client = TestClient(server.app, raise_server_exceptions=False)
    scope = {
        "domain_mode": "appearance_only",
        "segmentation_mode": "existing_coordinates",
        "sample_scope": {"kind": "all_accepted"},
    }

    preflight_response = client.post(
        "/api/maintenance/reextract-sample-images/preflight",
        json={"scope": scope},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["enabled"] is True
    assert preflight["summary"]["targets"] == 1
    assert preflight["summary"]["expected_candidates"] == 1
    assert preflight["summary"]["blocked"] == 0

    candidate_response = client.post(
        "/api/maintenance/reextract-sample-images/candidate-sets",
        json={"scope": scope},
    )
    assert candidate_response.status_code == 200, candidate_response.text
    candidate_set_id = candidate_response.json()["candidate_set_id"]

    review_response = client.post(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/exp-001/review",
        json={"decision": "save"},
    )
    assert review_response.status_code == 200, review_response.text

    apply_response = client.post(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/apply",
        json={},
    )
    assert apply_response.status_code == 200, apply_response.text
    result = apply_response.json()["report"]
    assert result["status"] == "completed"
    assert result["summary"]["applied_changed"] == 1
    assert result["summary"]["failed"] == 0
    assert result["stale_model_fit_ids"] == ["fit-ct"]
    after = store.get_extraction_result("exp-001")
    assert after["measurements"]["swatches"][0]["appearance"] == {
        "source": maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
        "jpeg_r": 10.0,
        "jpeg_g": 20.0,
        "jpeg_b": 30.0,
        "swatch_box": None,
    }
    assert after["diagnostics"]["decode_environment"] == {"rawpy": "new"}
    assert store.get_model_fit("fit-ct")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-photo")["currentness_state"] == "current"


def test_maintenance_reextract_candidate_generation_and_model_fit_respect_extraction_writer_lock(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    acquired, blocker = server._try_begin_extraction_writer(  # type: ignore[attr-defined]
        "test_extraction_writer",
        job_id="writer-lock",
        operation_id="test_extraction_writer",
    )
    assert acquired, blocker
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        reextract_response = client.post(
            "/api/maintenance/reextract-sample-images/candidate-sets",
            json={
                "scope": {
                    "domain_mode": "appearance_only",
                    "segmentation_mode": "existing_coordinates",
                    "sample_scope": {"kind": "all_accepted"},
                }
            },
        )
        fit_response = client.post(
            "/api/maintenance/jobs",
            json={"operation_id": "refit_calibration_models"},
        )

        assert reextract_response.status_code == 409, reextract_response.text
        assert "test extraction writer" in reextract_response.text.lower()
        assert fit_response.status_code == 409, fit_response.text
        assert "test extraction writer" in fit_response.text.lower()
    finally:
        server._end_extraction_writer(kind="test_extraction_writer", job_id="writer-lock")  # type: ignore[attr-defined]


def test_maintenance_regenerates_managed_geometry_artifacts_without_public_export(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry_id = store.list_geometry_definitions()[0].geometry_id

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "regenerate_managed_step_artifacts", "mode": "missing_only"},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["enabled"] is True
    assert preflight["summary"]["targets"] >= 1
    assert "Prisma/output" in preflight["warnings"][0]

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "regenerate_managed_step_artifacts",
            "mode": "missing_only",
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    assert job_payload["result"]["summary"]["regenerated_geometries"] >= 1
    summary = store.get_geometry_artifact_summary(geometry_id)
    assert summary["manifest_exists"] is True
    assert summary["step_paths"]
    assert summary["stl_paths"]
    assert not store.step_export_dir.exists() or not list(store.step_export_dir.rglob("*"))
    refresh = job_payload["result"]["ui_refresh"]
    assert refresh["kind"] == "targeted"
    assert refresh["reload_app_data"] is True
    assert geometry_id in refresh["invalidate_geometry_artifacts"]["geometry_ids"]

    current_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "regenerate_managed_step_artifacts", "mode": "missing_only"},
    )
    assert current_response.status_code == 200, current_response.text
    assert current_response.json()["preflight"]["summary"]["targets"] == 0


def test_missing_geometry_audit_and_regeneration_share_health_classification(tmp_path: Path) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=False,
        export_step_file=True,
        export_stl_files=True,
    )
    summary = store.get_geometry_artifact_summary(geometry_id)
    Path(summary["stl_paths"][0]).unlink()
    Path(summary["manifest_path"]).write_text("not valid JSON", encoding="utf-8")

    audit = maintenance.execute_operation(store, "audit_missing_artifacts")
    audit_categories = {
        str(item.get("category"))
        for item in audit["findings"]
        if item.get("geometry_id") == geometry_id
    }
    plan = maintenance._geometry_artifact_regeneration_targets(store, "missing_only")  # type: ignore[attr-defined]
    planned = next(item for item in plan["work"] if item["geometry_id"] == geometry_id)

    assert "geometry_manifest_unreadable" in audit_categories
    assert "geometry_body_labels_missing" in audit_categories
    assert set(planned["health_categories"]) == audit_categories


def test_missing_geometry_audit_reports_manifest_absent_even_when_artifacts_exist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=False,
        export_step_file=True,
        export_stl_files=True,
    )
    summary = store.get_geometry_artifact_summary(geometry_id)
    Path(summary["manifest_path"]).unlink()

    refreshed = store.get_geometry_artifact_summary(geometry_id)
    audit = maintenance.execute_operation(store, "audit_missing_artifacts")

    assert refreshed["manifest_exists"] is False
    assert refreshed["step_paths"]
    assert refreshed["stl_paths"]
    assert any(
        item.get("category") == "geometry_manifest_missing"
        and item.get("geometry_id") == geometry_id
        for item in audit["findings"]
    )


def test_quarantine_moves_only_noncurrent_geometry_fingerprint_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=False,
        export_step_file=True,
        export_stl_files=True,
    )
    summary = store.get_geometry_artifact_summary(geometry_id)
    current_dir = Path(summary["artifact_root"])
    stale_fingerprint = "a" * 64 if current_dir.name != "a" * 64 else "b" * 64
    stale_dir = current_dir.parent / stale_fingerprint
    stale_dir.mkdir(parents=True)
    (stale_dir / "obsolete.step").write_bytes(b"obsolete managed geometry")
    public_file = store.step_export_dir / "user-created.step"
    public_file.parent.mkdir(parents=True, exist_ok=True)
    public_file.write_bytes(b"unrelated public export")

    findings = maintenance._collect_orphan_artifact_findings(store)  # type: ignore[attr-defined]
    stale_finding = next(
        item for item in findings
        if item.get("category") == "orphan_geometry_fingerprint_dir"
        and Path(str(item.get("path"))).resolve() == stale_dir.resolve()
    )
    report = maintenance._execute_quarantine_orphaned_artifacts(  # type: ignore[attr-defined]
        store,
        "cleanup",
        {},
        progress_cb=None,
        should_cancel=None,
    )

    assert not stale_dir.exists()
    assert current_dir.exists()
    assert public_file.read_bytes() == b"unrelated public export"
    moved = next(
        item for item in report["findings"]
        if item.get("category") == "orphan_geometry_fingerprint_dir"
    )
    assert Path(moved["quarantine_path"]).is_dir()

    stale_dir.mkdir(parents=True)
    original_summary = store.get_geometry_artifact_summary

    def now_current(value):  # type: ignore[no-untyped-def]
        payload = dict(original_summary(value))
        payload["structural_fingerprint"] = stale_fingerprint
        return payload

    store.get_geometry_artifact_summary = now_current  # type: ignore[method-assign]
    assert maintenance._orphan_target_still_eligible(  # type: ignore[attr-defined]
        store,
        stale_finding,
        stale_dir,
    ) is False


def test_geometry_generation_failure_leaves_live_managed_and_public_files_unchanged(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=True,
        export_stl_files=True,
        export_name="Transactional Export",
        overwrite_public_export=True,
    )
    before = _tree_file_snapshot(store.system_dir, store.step_export_dir)

    def fail_after_partial_stage(_definition, output_dir, **_kwargs):  # type: ignore[no-untyped-def]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "partial.step").write_bytes(b"partial staged geometry")
        raise RuntimeError("injected geometry generation failure")

    monkeypatch.setattr(sqlite_data_access, "export_geometry_artifacts", fail_after_partial_stage)

    with pytest.raises(RuntimeError, match="injected geometry generation failure"):
        store.generate_geometry_artifacts(
            geometry_id,
            export_to_output=True,
            export_step_file=True,
            export_stl_files=True,
            export_name="Transactional Export",
            overwrite_public_export=True,
        )

    assert _tree_file_snapshot(store.system_dir, store.step_export_dir) == before
    assert not list((store.system_dir / "geometry_artifacts" / geometry_id).glob(".geometry-stage-*"))


def test_geometry_publication_failure_rolls_back_every_replaced_path(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    initial = store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=True,
        export_stl_files=True,
        export_name="Transactional Export",
        overwrite_public_export=True,
    )
    public_step = Path(initial["latest_export_paths"]["step"])
    before = _tree_file_snapshot(store.system_dir, store.step_export_dir)
    real_replace = sqlite_data_access.os.replace
    failed = False

    def fail_public_step_promotion(src, dst):  # type: ignore[no-untyped-def]
        nonlocal failed
        if (
            not failed
            and Path(dst).resolve() == public_step.resolve()
            and ".geometry-stage-" in Path(src).name
        ):
            failed = True
            raise OSError("injected public STEP promotion failure")
        return real_replace(src, dst)

    monkeypatch.setattr(sqlite_data_access.os, "replace", fail_public_step_promotion)

    with pytest.raises(RuntimeError, match="geometry publication failed"):
        store.generate_geometry_artifacts(
            geometry_id,
            export_to_output=True,
            export_step_file=True,
            export_stl_files=True,
            export_name="Transactional Export",
            overwrite_public_export=True,
        )

    assert failed is True
    assert _tree_file_snapshot(store.system_dir, store.step_export_dir) == before
    assert not list(store.system_dir.rglob(".geometry-rollback-*"))
    assert not list(store.step_export_dir.rglob(".geometry-rollback-*"))


def test_geometry_manifest_staging_failure_does_not_publish_files(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=True,
        export_stl_files=True,
        export_name="Transactional Export",
        overwrite_public_export=True,
    )
    before = _tree_file_snapshot(store.system_dir, store.step_export_dir)
    real_write = store._atomic_write_json  # type: ignore[attr-defined]

    def fail_staged_manifest(path, payload):  # type: ignore[no-untyped-def]
        if ".geometry-stage-" in Path(path).parent.name:
            Path(path).write_text("partial manifest", encoding="utf-8")
            raise OSError("injected manifest staging failure")
        return real_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write_json", fail_staged_manifest)

    with pytest.raises(OSError, match="injected manifest staging failure"):
        store.generate_geometry_artifacts(
            geometry_id,
            export_to_output=True,
            export_step_file=True,
            export_stl_files=True,
            export_name="Transactional Export",
            overwrite_public_export=True,
        )

    assert _tree_file_snapshot(store.system_dir, store.step_export_dir) == before


def test_geometry_staging_copy_failure_does_not_publish_files(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=True,
        export_stl_files=True,
        export_name="Transactional Export",
        overwrite_public_export=True,
    )
    before = _tree_file_snapshot(store.system_dir, store.step_export_dir)
    real_copy = sqlite_data_access.shutil.copy2
    failed = False

    def fail_first_transaction_stage_copy(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed and ".geometry-stage-" in Path(dst).name:
            failed = True
            raise OSError("injected geometry staging copy failure")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(sqlite_data_access.shutil, "copy2", fail_first_transaction_stage_copy)

    with pytest.raises(OSError, match="injected geometry staging copy failure"):
        store.generate_geometry_artifacts(
            geometry_id,
            export_to_output=True,
            export_step_file=True,
            export_stl_files=True,
            export_name="Transactional Export",
            overwrite_public_export=True,
        )

    assert failed is True
    assert _tree_file_snapshot(store.system_dir, store.step_export_dir) == before


def test_geometry_force_export_removes_only_manifest_owned_stale_stls(tmp_path: Path) -> None:
    store = _store(tmp_path)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    result = store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=False,
        export_stl_files=True,
        export_name="Owned STL Export",
        overwrite_public_export=True,
    )
    export_dir = Path(result["latest_export_paths"]["stl"])
    owned_stale = export_dir / "Owned_STL_Export_retired-role.stl"
    unrelated = export_dir / "user-created.stl"
    owned_stale.write_bytes(b"old Prisma-owned STL")
    unrelated.write_bytes(b"user STL")
    summary = store.get_geometry_artifact_summary(geometry_id)
    manifest_path = Path(summary["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["latest_stl_export_files"] = [
        *manifest.get("latest_stl_export_files", []),
        str(owned_stale.resolve()),
    ]
    store._atomic_write_json(manifest_path, manifest)  # type: ignore[attr-defined]

    store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_step_file=False,
        export_stl_files=True,
        export_name="Owned STL Export",
        overwrite_public_export=True,
    )

    assert not owned_stale.exists()
    assert unrelated.read_bytes() == b"user STL"


def test_cancelled_geometry_regeneration_does_not_enter_publication(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("cancelled maintenance must not start geometry generation")

    monkeypatch.setattr(store, "generate_geometry_artifacts", forbidden)
    report = maintenance._execute_regenerate_managed_geometry_artifacts(  # type: ignore[attr-defined]
        store,
        "force",
        {},
        progress_cb=None,
        should_cancel=lambda: True,
    )

    assert report["status"] == "cancelled"
    assert report["summary"]["regenerated_geometries"] == 0


def test_maintenance_quarantines_orphaned_artifacts_without_deleting_them(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    orphan_preview = store.root / "previews" / "orphan_preview_small.jpg"
    orphan_preview.parent.mkdir(parents=True, exist_ok=True)
    orphan_preview.write_bytes(b"orphan preview")
    orphan_thumbnail_dir = store.root / "thumbnails" / "exp-orphan"
    orphan_thumbnail_dir.mkdir(parents=True, exist_ok=True)
    orphan_thumbnail_file = orphan_thumbnail_dir / "mock.jpg"
    orphan_thumbnail_file.write_bytes(b"orphan thumbnail")
    interrupted_thumbnail = store.root / "thumbnails" / "exp-001" / ".source.stage.interrupted.jpg"
    interrupted_thumbnail.parent.mkdir(parents=True, exist_ok=True)
    interrupted_thumbnail.write_bytes(b"interrupted thumbnail transaction")
    orphan_geometry_dir = store.root / "_system" / "geometry_artifacts" / "geom-orphan"
    orphan_geometry_dir.mkdir(parents=True, exist_ok=True)
    orphan_geometry_file = orphan_geometry_dir / "manifest.json"
    orphan_geometry_file.write_text("{}", encoding="utf-8")

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "quarantine_orphaned_artifacts", "mode": "cleanup"},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["enabled"] is True
    assert preflight["summary"]["targets"] >= 3
    assert preflight["summary"]["writes"] >= 3
    assert any("Current targets are moved" in warning for warning in preflight["warnings"])

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "quarantine_orphaned_artifacts",
            "mode": "cleanup",
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    result = job_payload["result"]
    assert result["summary"]["moved"] >= 3
    assert result["summary"]["quarantine_path"]
    moved_by_original = {
        Path(item["original_path"]).name: Path(item["quarantine_path"])
        for item in result["findings"]
        if item.get("original_path")
    }
    assert "orphan_preview_small.jpg" in moved_by_original
    assert ".source.stage.interrupted.jpg" in moved_by_original
    assert "exp-orphan" in moved_by_original
    assert "geom-orphan" in moved_by_original
    assert not orphan_preview.exists()
    assert not orphan_thumbnail_dir.exists()
    assert not interrupted_thumbnail.exists()
    assert not orphan_geometry_dir.exists()
    assert moved_by_original["orphan_preview_small.jpg"].exists()
    assert (moved_by_original["exp-orphan"] / "mock.jpg").exists()
    assert moved_by_original[".source.stage.interrupted.jpg"].exists()
    assert (moved_by_original["geom-orphan"] / "manifest.json").exists()
    assert Path(result["summary"]["quarantine_path"]).is_dir()
    assert result["ui_refresh"]["kind"] == "none"

    audit_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "quarantine_orphaned_artifacts", "mode": "cleanup"},
    )
    assert audit_response.status_code == 200, audit_response.text
    assert audit_response.json()["preflight"]["summary"]["targets"] == 0


def test_orphan_cleanup_preserves_current_rotated_blank_previews(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blank = store.list_blanks()[0]
    assert store.set_image_rotation(blank.original_filename, 1) == 1
    preview_dir = store.root / "previews"
    preview_dir.mkdir(parents=True)
    current = {
        preview_dir / f"{blank.blank_id}__blank__r1.jpg",
        preview_dir / f"{blank.blank_id}__blank__r1_small.jpg",
    }
    stale = {
        preview_dir / f"{blank.blank_id}__blank.jpg",
        preview_dir / f"{blank.blank_id}__blank_small.jpg",
    }
    for path in current | stale:
        path.write_bytes(path.name.encode("ascii"))

    findings = maintenance._collect_orphan_artifact_findings(store)  # type: ignore[attr-defined]
    orphan_names = {
        Path(str(item.get("path") or "")).name
        for item in findings
        if item.get("category") == "orphan_preview"
    }

    assert orphan_names.isdisjoint(path.name for path in current)
    assert {path.name for path in stale}.issubset(orphan_names)

    report = maintenance._execute_quarantine_orphaned_artifacts(  # type: ignore[attr-defined]
        store,
        "cleanup",
        {},
        progress_cb=None,
        should_cancel=None,
    )

    assert all(path.exists() for path in current)
    assert all(not path.exists() for path in stale)
    assert report["summary"]["moved"] == 2


def test_orphan_audit_treats_cache_marker_like_source_stems_literally(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_image_asset(store, "img-literal-a", "literal_small.CR2")
    _add_image_asset(store, "img-literal-b", "literal__r1.CR2")
    assert store.set_image_rotation("literal__r1.CR2", 2) == 2
    preview_dir = store.root / "previews"
    preview_dir.mkdir(parents=True)
    literal_small_stem = maintenance.preview_cache_stem(store, "literal_small.CR2")
    literal_rotated_stem = maintenance.preview_cache_stem(store, "literal__r1.CR2")
    expected = {
        preview_dir / f"{literal_small_stem}.jpg",
        preview_dir / f"{literal_small_stem}_small.jpg",
        preview_dir / f"{literal_rotated_stem}.jpg",
        preview_dir / f"{literal_rotated_stem}_small.jpg",
    }
    legacy = {
        preview_dir / "literal_small.jpg",
        preview_dir / "literal_small_small.jpg",
        preview_dir / "literal__r1__r2.jpg",
        preview_dir / "literal__r1__r2_small.jpg",
    }
    for path in expected | legacy:
        path.write_bytes(path.name.encode("ascii"))

    findings = maintenance._collect_orphan_artifact_findings(store)  # type: ignore[attr-defined]
    orphan_names = {
        Path(str(item.get("path") or "")).name
        for item in findings
        if item.get("category") == "orphan_preview"
    }

    assert orphan_names.isdisjoint(path.name for path in expected)
    assert {path.name for path in legacy}.issubset(orphan_names)


def test_preview_cache_cleanup_removes_only_unowned_exact_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_image_asset(store, "img-prefix-a", "IMG_1.CR2")
    _add_image_asset(store, "img-prefix-b", "IMG_10.CR2")
    _add_image_asset(store, "img-blank-c", "blank-rotate.CR2")
    blank = store.register_blank_from_image("blank-rotate.CR2")
    assert store.set_image_rotation("IMG_1.CR2", 2) == 2
    assert store.set_image_rotation("blank-rotate.CR2", 3) == 3
    preview_dir = store.root / "previews"
    preview_dir.mkdir(parents=True)

    current_source_stem = maintenance.preview_cache_stem(store, "IMG_1.CR2")
    other_source_stem = maintenance.preview_cache_stem(store, "IMG_10.CR2")
    source_base_stem = current_source_stem.rsplit("__r2", 1)[0]
    stale_source = {preview_dir / f"{source_base_stem}.jpg", preview_dir / f"{source_base_stem}_small.jpg"}
    current_source = {preview_dir / f"{current_source_stem}.jpg", preview_dir / f"{current_source_stem}_small.jpg"}
    other_source = {preview_dir / f"{other_source_stem}.jpg", preview_dir / f"{other_source_stem}_small.jpg"}
    stale_blank = {
        preview_dir / f"{blank.blank_id}__blank.jpg",
        preview_dir / f"{blank.blank_id}__blank_small.jpg",
    }
    current_blank = {
        preview_dir / f"{blank.blank_id}__blank__r3.jpg",
        preview_dir / f"{blank.blank_id}__blank__r3_small.jpg",
    }
    for path in stale_source | current_source | other_source | stale_blank | current_blank:
        path.write_bytes(path.name.encode("ascii"))

    server._clear_preview_cache(store, "IMG_1.CR2")  # type: ignore[attr-defined]

    assert all(not path.exists() for path in stale_source)
    assert all(path.exists() for path in current_source | other_source)

    server._clear_preview_cache(store, "blank-rotate.CR2")  # type: ignore[attr-defined]

    assert all(not path.exists() for path in stale_blank)
    assert all(path.exists() for path in current_blank | other_source)


def test_orphan_cleanup_revalidates_each_target_before_move(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    orphan_dir = store.root / "thumbnails" / "exp-newly-live"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "source.jpg").write_bytes(b"derived")
    monkeypatch.setattr(maintenance, "_orphan_target_still_eligible", lambda *_args: False)

    report = maintenance._execute_quarantine_orphaned_artifacts(
        store,
        "cleanup",
        {},
        progress_cb=None,
        should_cancel=None,
    )

    assert orphan_dir.exists()
    assert report["summary"]["moved"] == 0
    assert report["summary"]["skipped_reactivated"] >= 1


def test_orphan_cleanup_blocks_directory_with_nested_filesystem_link(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    orphan_dir = store.root / "thumbnails" / "exp-linked-orphan"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "source.jpg").write_bytes(b"derived")
    real_tree_check = maintenance.tree_contains_link
    monkeypatch.setattr(
        maintenance,
        "tree_contains_link",
        lambda path: True if Path(path) == orphan_dir else real_tree_check(Path(path)),
    )

    targets, blocked = maintenance._partition_existing_orphan_targets(store)  # type: ignore[attr-defined]

    assert all(Path(str(item.get("path"))) != orphan_dir for item in targets)
    assert any(
        Path(str(item.get("path"))) == orphan_dir
        and "contains a filesystem link" in str(item.get("message"))
        for item in blocked
    )
    assert orphan_dir.exists()


def test_quarantine_prune_removes_only_expired_managed_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = maintenance.quarantine_dir(store)
    old_run = root / "20260101_000000_000001"
    recent_run = root / "20260101_000000_000002"
    unmanaged = root / "notes"
    for path in (old_run, recent_run, unmanaged):
        path.mkdir(parents=True)
        (path / "artifact.txt").write_text("derived", encoding="utf-8")
    now = time.time()
    os.utime(old_run, (now - maintenance.QUARANTINE_RETENTION_SECONDS - 1, now - maintenance.QUARANTINE_RETENTION_SECONDS - 1))

    result = maintenance.prune_quarantine_runs(store, now=now)

    assert str(old_run) in result["removed"]
    assert not old_run.exists()
    assert recent_run.exists()
    assert str(recent_run) in result["deferred"]
    assert unmanaged.exists()
    assert str(unmanaged) in result["skipped"]


def test_maintenance_force_geometry_artifacts_preserves_public_exports(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry_id = store.list_geometry_definitions()[0].geometry_id
    public_manifest = store.generate_geometry_artifacts(
        geometry_id,
        export_to_output=True,
        export_name="User Export",
        export_step_file=True,
        export_stl_files=False,
    )
    public_step = store.step_export_dir / "User_Export.step"
    assert public_step.exists()
    public_step.write_text("user-facing export sentinel", encoding="utf-8")
    manifest_path = Path(public_manifest["step_path"]).parent / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload.pop("latest_export_paths", None)
    manifest_payload.pop("latest_stl_export_files", None)
    manifest_payload["export_paths"] = [str(public_step.resolve())]
    manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "regenerate_managed_step_artifacts", "mode": "force"},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "regenerate_managed_step_artifacts",
            "mode": "force",
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    assert public_step.read_text(encoding="utf-8") == "user-facing export sentinel"
    summary = store.get_geometry_artifact_summary(geometry_id)
    assert Path(summary["latest_step_export_path"]).name == Path(public_manifest["export_paths"][0]).name
    assert Path(summary["latest_step_export_path"]).name == "User_Export.step"
    assert [Path(path).name for path in summary["export_paths"]] == ["User_Export.step"]


def test_maintenance_exports_missing_public_geometry_step_files(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry = store.list_geometry_definitions()[0]
    public_step = store.step_export_dir / f"{maintenance._safe_export_stem(geometry.alias)}.step"  # type: ignore[attr-defined]
    assert not public_step.exists()

    scope = {"geometry_scope": "all_geometries", "output_types": ["step"]}
    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "export_geometry_files", "mode": "missing_only", "scope": scope},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["enabled"] is True
    assert preflight["summary"]["writes"] >= 1
    assert preflight["summary"]["overwrite_candidates"] == 0

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "export_geometry_files",
            "mode": "missing_only",
            "scope": scope,
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    assert public_step.exists()
    assert job_payload["result"]["summary"]["exported_geometries"] >= 1
    assert str(public_step.resolve()) in job_payload["result"]["changed_paths"]
    refresh = job_payload["result"]["ui_refresh"]
    assert refresh["kind"] == "targeted"
    assert refresh["reload_app_data"] is True
    assert geometry.geometry_id in refresh["invalidate_geometry_artifacts"]["geometry_ids"]


def test_maintenance_exports_missing_public_geometry_stl_files(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry = store.list_geometry_definitions()[0]

    scope = {"geometry_scope": "all_geometries", "output_types": ["stl"]}
    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "export_geometry_files", "mode": "missing_only", "scope": scope},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    stl_item = next(
        item for item in preflight["export_items"]
        if item["geometry_id"] == geometry.geometry_id and item["kind"] == "stl"
    )
    expected_files = [Path(path) for path in stl_item["expected_files"]]
    assert expected_files
    assert not any(path.exists() for path in expected_files)

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "export_geometry_files",
            "mode": "missing_only",
            "scope": scope,
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "succeeded"
    assert all(path.exists() for path in expected_files)
    summary = store.get_geometry_artifact_summary(geometry.geometry_id)
    assert summary["latest_stl_export_path"]
    assert set(summary["latest_stl_export_files"]) == {str(path.resolve()) for path in expected_files}


def test_maintenance_export_geometry_force_requires_confirmation_before_overwrite(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry = store.list_geometry_definitions()[0]
    public_step = store.step_export_dir / f"{maintenance._safe_export_stem(geometry.alias)}.step"  # type: ignore[attr-defined]
    store.generate_geometry_artifacts(
        geometry.geometry_id,
        export_to_output=True,
        export_step_file=True,
        export_stl_files=False,
    )
    public_step.write_text("public sentinel", encoding="utf-8")

    scope = {"geometry_scope": "all_geometries", "output_types": ["step"]}
    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "export_geometry_files", "mode": "force", "scope": scope},
    )
    assert preflight_response.status_code == 200, preflight_response.text
    preflight = preflight_response.json()["preflight"]
    assert preflight["summary"]["requires_confirmation"] is True
    assert preflight["summary"]["overwrite_candidates"] >= 1
    assert preflight["required_confirmation"] == "Overwrite existing geometry exports"

    blocked_job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "export_geometry_files",
            "mode": "force",
            "scope": scope,
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert blocked_job_response.status_code == 200, blocked_job_response.text
    blocked_job = _wait_for_maintenance_job(client, blocked_job_response.json()["job_id"])
    assert blocked_job["status"] == "failed"
    assert public_step.read_text(encoding="utf-8") == "public sentinel"

    confirmed_preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "export_geometry_files", "mode": "force", "scope": scope},
    )
    assert confirmed_preflight_response.status_code == 200, confirmed_preflight_response.text
    confirmed_job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "export_geometry_files",
            "mode": "force",
            "scope": scope,
            "preflight_token": confirmed_preflight_response.json()["preflight_token"],
            "confirmation": "Overwrite existing geometry exports",
        },
    )
    assert confirmed_job_response.status_code == 200, confirmed_job_response.text
    assert "confirmation" not in confirmed_job_response.json()
    confirmed_job = _wait_for_maintenance_job(client, confirmed_job_response.json()["job_id"])
    assert confirmed_job["status"] == "succeeded"
    assert "confirmation" not in confirmed_job
    assert public_step.read_text(encoding="utf-8") != "public sentinel"


def test_maintenance_export_geometry_rejects_stale_preflight_after_alias_change(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)
    geometry = store.list_geometry_definitions()[0]
    original_public_step = store.step_export_dir / f"{maintenance._safe_export_stem(geometry.alias)}.step"  # type: ignore[attr-defined]

    scope = {"geometry_scope": "all_geometries", "output_types": ["step"]}
    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={"operation_id": "export_geometry_files", "mode": "missing_only", "scope": scope},
    )
    assert preflight_response.status_code == 200, preflight_response.text

    store.update_geometry_metadata(geometry.geometry_id, alias=f"{geometry.alias} changed")
    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "export_geometry_files",
            "mode": "missing_only",
            "scope": scope,
            "preflight_token": preflight_response.json()["preflight_token"],
        },
    )
    assert job_response.status_code == 200, job_response.text
    job_payload = _wait_for_maintenance_job(client, job_response.json()["job_id"])

    assert job_payload["status"] == "failed"
    assert "plan changed" in "; ".join(job_payload["result"]["errors"]).lower()
    assert not original_public_step.exists()


def test_maintenance_rejects_interactive_operations_on_generic_job_endpoint(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        "/api/maintenance/jobs",
        json={"operation_id": "reextract_sample_images", "mode": "reextract"},
    )

    assert response.status_code == 400
    assert "dedicated workflow" in response.text


def test_reextract_sample_images_preflight_endpoint_reports_appearance_plan(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        "/api/maintenance/reextract-sample-images/preflight",
        json={
            "scope": {
                "domain_mode": "appearance_only",
                "segmentation_mode": "existing_coordinates",
                "sample_scope": {"kind": "all_accepted"},
            }
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["preflight"]["operation_id"] == "reextract_sample_images"
    assert payload["preflight"]["enabled"] is True
    assert payload["preflight"]["summary"]["domain_mode"] == "appearance_only"
    assert payload["preflight"]["summary"]["segmentation_mode"] == "existing_coordinates"


def test_validate_data_endpoint_surface_stays_retired(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    retired_calls = [
        client.post("/api/maintenance/validate-data"),
        client.post("/api/maintenance/validate-data/start"),
        client.get("/api/maintenance/validate-data/status/not-a-job"),
        client.post("/api/maintenance/ensure-derived-assets"),
    ]
    assert all(response.status_code in {404, 405} for response in retired_calls)


def test_maintenance_preflight_token_is_bound_to_scope(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    preflight_response = client.post(
        "/api/maintenance/preflight",
        json={
            "operation_id": "sqlite_integrity_check",
            "scope": {"sample_id": "exp-001"},
        },
    )
    assert preflight_response.status_code == 200, preflight_response.text

    job_response = client.post(
        "/api/maintenance/jobs",
        json={
            "operation_id": "sqlite_integrity_check",
            "preflight_token": preflight_response.json()["preflight_token"],
            "scope": {"sample_id": "exp-002"},
        },
    )

    assert job_response.status_code == 400
    assert "selected scope" in job_response.text
