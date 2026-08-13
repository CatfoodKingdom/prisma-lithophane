import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import data_paths
from guide_runtime import GuideRuntimeStore
import server


def _runtime_headers(page_id: str, *, session_id: str | None = None, epoch: int = 0) -> dict:
    headers = {
        "X-Prisma-Page-Id": page_id,
        "X-Prisma-Workspace-Epoch": str(epoch),
    }
    if session_id:
        headers["X-Prisma-Guide-Session"] = session_id
    return headers


def _begin_runtime(client: TestClient, page_id: str = "owner-page") -> str:
    acquired = client.post(
        "/api/guides/runtime/acquire",
        json={"page_id": page_id},
        headers=_runtime_headers(page_id),
    )
    assert acquired.status_code == 200
    lease_id = acquired.json()["lease"]["lease_id"]
    begun = client.post(
        "/api/guides/runtime/begin",
        json={
            "page_id": page_id,
            "lease_id": lease_id,
            "guide_id": "prisma-generator-basics",
            "route_id": "full",
            "client_snapshot": {},
        },
        headers=_runtime_headers(page_id),
    )
    assert begun.status_code == 200
    return begun.json()["session_id"]


def test_begin_runtime_reports_the_resolved_images_folder(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    images_dir = tmp_path / "Images"
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    monkeypatch.setattr(server, "_IMAGES_DIR", images_dir)
    client = TestClient(server.app)
    acquired = client.post(
        "/api/guides/runtime/acquire",
        json={"page_id": "owner-page"},
        headers=_runtime_headers("owner-page"),
    ).json()

    begun = client.post(
        "/api/guides/runtime/begin",
        json={
            "page_id": "owner-page",
            "lease_id": acquired["lease"]["lease_id"],
            "guide_id": "prisma-generator-basics",
            "route_id": "full",
            "client_snapshot": {},
        },
        headers=_runtime_headers("owner-page"),
    )

    assert begun.status_code == 200
    assert begun.json()["images_folder"] == str(images_dir)


def test_guide_lease_locks_other_windows_and_epoch_invalidates_stale_owner(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    monkeypatch.setattr(server, "_restore_guide_server_snapshot", lambda _snapshot: {"ok": True})
    client = TestClient(server.app)
    session_id = _begin_runtime(client)

    blocked = client.post(
        "/api/session/config",
        json={},
        headers=_runtime_headers("other-page"),
    )
    assert blocked.status_code == 423
    assert "Another Prisma window" in blocked.json()["detail"]

    allowed = client.post(
        "/api/session/config",
        json={},
        headers=_runtime_headers("owner-page", session_id=session_id),
    )
    assert allowed.status_code == 200

    restored = client.post(
        "/api/guides/runtime/restore-server",
        json={"page_id": "owner-page", "session_id": session_id},
        headers=_runtime_headers("owner-page", session_id=session_id),
    )
    assert restored.status_code == 200
    blocked_during_recovery = client.post(
        "/api/session/config",
        json={},
        headers=_runtime_headers("owner-page", session_id=session_id),
    )
    assert blocked_during_recovery.status_code == 423
    assert "restoring" in blocked_during_recovery.json()["detail"].lower()
    finalized = client.post(
        "/api/guides/runtime/finalize",
        json={"page_id": "owner-page", "session_id": session_id},
        headers=_runtime_headers("owner-page", session_id=session_id),
    )
    assert finalized.json()["workspace_epoch"] == 1
    stale = client.post(
        "/api/session/config",
        json={},
        headers=_runtime_headers("owner-page", epoch=0),
    )
    assert stale.status_code == 423
    assert "stale" in stale.json()["detail"].lower()


def test_departure_endpoint_allows_replacement_page_to_claim_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    client = TestClient(server.app)
    session_id = _begin_runtime(client)

    departed = client.post(
        "/api/guides/runtime/depart",
        json={"page_id": "owner-page"},
        headers=_runtime_headers("owner-page", session_id=session_id),
    )
    claimed = client.post(
        "/api/guides/runtime/claim-recovery",
        json={"page_id": "replacement-page", "session_id": session_id},
        headers=_runtime_headers("replacement-page"),
    )

    assert departed.status_code == 200
    assert departed.json()["lease"] is None
    assert departed.json()["session"]["session_id"] == session_id
    assert claimed.status_code == 200
    assert claimed.json()["phase"] == "restoring"


def test_guide_action_idempotency_replays_one_server_job_start(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    server._GUIDE_IDEMPOTENCY_RESULTS.clear()
    server._GUIDE_IDEMPOTENCY_LOCKS.clear()
    starts: list[str] = []

    def fake_start(_payload) -> dict:
        starts.append("called")
        return {"job_id": f"job-{len(starts)}", "status": "running"}

    monkeypatch.setattr(server, "_start_full_solve_job", fake_start)
    client = TestClient(server.app)
    session_id = _begin_runtime(client)
    headers = {
        **_runtime_headers("owner-page", session_id=session_id),
        "X-Prisma-Idempotency-Key": "guide:step:complete:0:solve.single",
    }

    first = client.post("/api/solve/start", json={}, headers=headers)
    second = client.post("/api/solve/start", json={"card_id": "retry-card"}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"job_id": "job-1", "status": "running"}
    assert starts == ["called"]


def test_begin_rejects_malformed_client_snapshot_before_durable_record(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    client = TestClient(server.app)
    acquired = client.post(
        "/api/guides/runtime/acquire",
        json={"page_id": "owner-page"},
        headers=_runtime_headers("owner-page"),
    ).json()

    response = client.post(
        "/api/guides/runtime/begin",
        json={
            "page_id": "owner-page",
            "lease_id": acquired["lease"]["lease_id"],
            "guide_id": "prisma-generator-basics",
            "route_id": "full",
            "client_snapshot": {
                "enabled_filaments": {
                    "runtime_library_id": "library",
                    "enabled_ids": ["valid", 7],
                }
            },
        },
        headers=_runtime_headers("owner-page"),
    )

    assert response.status_code == 422
    assert not store.path.exists()


def test_ghost_printer_is_session_only_and_never_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    printers_path = tmp_path / "printers.json"
    persisted = deepcopy(server._DEFAULT_PRINTERS)
    printers_path.write_text(json.dumps(persisted), encoding="utf-8")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", runtime_store)
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    server.session["guide"].update({
        "mounted_asset_ids": set(),
        "ghost_printer_mounted": False,
        "printer_setup_overlay": None,
    })
    client = TestClient(server.app)
    session_id = _begin_runtime(client)
    headers = _runtime_headers("owner-page", session_id=session_id)

    mounted = client.post(
        "/api/guides/runtime/mount-printer",
        json={"page_id": "owner-page", "session_id": session_id},
        headers=headers,
    )
    assert mounted.status_code == 200
    profile = mounted.json()["profile"]
    assert profile["guide_only"] is True
    assert profile["editable"] is False

    selection_payload = {
        "expected_revision": persisted["revision"],
        "active_printer_id": "tutorial-printer",
        "active_nozzle_id": "nozzle-400",
        "current_width_um": 400,
        "intent_kind": "select_extrusion_width",
        "mutation_id": "guide-select-extrusion-width",
        "review_policy": "guide_authorized",
    }
    preview = client.put(
        "/api/printers/active",
        json=selection_payload,
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "review_required"

    selected = client.put(
        "/api/printers/active",
        json={
            **selection_payload,
            "acceptance_token": preview.json()["acceptance_token"],
        },
        headers=headers,
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "applied"
    assert selected.json()["printer"]["id"] == "tutorial-printer"
    assert selected.json()["nozzle"]["diameter_um"] == 400
    assert selected.json()["extrusion_width"]["width_um"] == 400

    disk = json.loads(printers_path.read_text(encoding="utf-8"))
    assert disk["active_printer_id"] == "bambu-x1c"
    assert all(item["id"] != "tutorial-printer" for item in disk["printers"])
    server.session["guide"]["ghost_printer_mounted"] = False


def test_protected_source_requires_mount_and_resolves_private_packaged_file() -> None:
    asset = server._GUIDE_ASSET_CATALOG.get("bubba-blanket")
    source_ref = "guide-image:bubba-blanket"
    display_name = asset["guide_display_name"]
    server.session["guide"]["mounted_asset_ids"].discard("bubba-blanket")

    try:
        server._resolve_image_source_path(display_name, source_ref)
    except server.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unmounted guide source unexpectedly resolved")

    server.session["guide"]["mounted_asset_ids"].add("bubba-blanket")
    try:
        assert server._resolve_image_source_path(display_name, source_ref) == Path(asset["path"])
        provenance = server._source_provenance_for_config(
            {"image_source_ref": source_ref, "image_path": display_name},
            server._resolve_run_source_image(Path(asset["path"]), prepare=False),
        )
        assert provenance["guide_asset_id"] == "bubba-blanket"
    finally:
        server.session["guide"]["mounted_asset_ids"].discard("bubba-blanket")


def test_workspace_reset_purges_project_work_but_preserves_durable_user_data(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    runs = cache / "runs"
    auto_runs = cache / "auto_runs"
    batches = cache / "palette-batches"
    luts = cache / "luts"
    source_images = cache / "source-images"
    images = tmp_path / "images"
    exports = tmp_path / "exports"
    saved_runs = tmp_path / "saved-runs"
    for directory in (
        runs,
        auto_runs,
        batches,
        luts,
        source_images,
        images,
        exports,
        saved_runs,
    ):
        directory.mkdir(parents=True)
        (directory / "marker.bin").write_bytes(b"keep-or-purge")

    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", runs)
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto_runs)
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", luts)
    monkeypatch.setattr(data_paths, "SOURCE_IMAGE_CACHE_DIR", source_images)
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved_runs)
    monkeypatch.setattr(data_paths, "UPLOAD_DIR", images)
    monkeypatch.setattr(data_paths, "OUTPUT_DIR", exports)
    monkeypatch.setattr(server, "_assert_no_active_job", lambda **_kwargs: None)

    original_session = deepcopy(server.session)
    try:
        server.session["solve_cache"] = {"card": {"status": "complete"}}
        server.session["config"]["image_path"] = "user-image.jpg"
        server.session["config"]["image_source_ref"] = None
        server.session["config"]["palette"] = ["filament-a"]
        server.session["guide"].update({
            "mounted_asset_ids": {"bubba-blanket"},
            "ghost_printer_mounted": True,
            "printer_setup_overlay": {
                "active_printer_id": "tutorial-printer",
                "printer_setup_state": {"tutorial-printer": deepcopy(server._TUTORIAL_PRINTER_SETUP_STATE)},
            },
        })

        result = server._reset_guide_backend_workspace()

        assert result["removed"] == 3
        assert not any(runs.iterdir())
        assert not any(auto_runs.iterdir())
        assert not any(batches.iterdir())
        for preserved in (luts, source_images, images, exports, saved_runs):
            assert (preserved / "marker.bin").read_bytes() == b"keep-or-purge"
        assert server.session["solve_cache"] == {}
        assert server.session["config"]["image_path"] is None
        assert server.session["config"]["palette"] == []
        assert server.session["guide"]["mounted_asset_ids"] == set()
        assert server.session["guide"]["ghost_printer_mounted"] is False
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_server_snapshot_restores_modules_and_settings_without_rewriting_printers(
    tmp_path: Path, monkeypatch
) -> None:
    printers_path = tmp_path / "printers.json"
    modules_path = tmp_path / "modules.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    original_session = deepcopy(server.session)
    try:
        printers = deepcopy(server._DEFAULT_PRINTERS)
        server._save_printers(printers)
        original_modules = server.load_module_state(modules_path)
        module_id = next(iter(original_modules))
        original_modules[module_id] = not original_modules[module_id]
        server.save_module_state(modules_path, original_modules)
        server.session["config"]["solve_pitch_extrusion_width_multiplier"] = 2
        snapshot = server._capture_guide_server_snapshot()

        changed = server._load_printers()
        changed["printer_setup_state"]["bambu-x1c"]["active_nozzle_id"] = "nozzle-400"
        changed["printer_setup_state"]["bambu-x1c"]["nozzle_width_state"]["nozzle-400"]["current_width_um"] = 450
        server._save_printers(changed)
        changed_modules = dict(original_modules)
        changed_modules[module_id] = not changed_modules[module_id]
        server.save_module_state(modules_path, changed_modules)
        server.session["config"]["solve_pitch_extrusion_width_multiplier"] = 1

        server._restore_guide_server_snapshot(snapshot)

        restored_printers = server._load_printers()
        assert restored_printers["active_printer_id"] == "bambu-x1c"
        assert restored_printers["printer_setup_state"]["bambu-x1c"]["active_nozzle_id"] == "nozzle-400"
        assert restored_printers["printer_setup_state"]["bambu-x1c"]["nozzle_width_state"]["nozzle-400"]["current_width_um"] == 450
        assert server.load_module_state(modules_path) == snapshot["modules"]
        assert server.session["config"]["solve_pitch_extrusion_width_multiplier"] == 2
        assert server.session["config"]["solver_fine_pitch_mm"] == pytest.approx(0.9)
        assert server.session["config"]["image_path"] is None
        assert server.session["config"]["palette"] == []
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_resource_reconciliation_uses_authoritative_identity_and_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    monkeypatch.setattr(server, "_GUIDE_RUNTIME_STORE", store)
    client = TestClient(server.app)
    session_id = _begin_runtime(client)
    headers = _runtime_headers("owner-page", session_id=session_id)
    resource = {
        "operation_id": "saving-loading-palette",
        "kind": "palette",
        "id": None,
        "name": "Saving & Loading Palette",
        "fingerprint": ["cyan", "magenta", "yellow"],
        "status": "pending_create",
    }
    transitioned = client.post(
        "/api/guides/runtime/resources",
        json={"page_id": "owner-page", "session_id": session_id, "resource": resource},
        headers=headers,
    )
    assert transitioned.status_code == 200
    monkeypatch.setattr(
        server,
        "_guide_resource_candidates",
        lambda kind: [{
            "id": "authoritative-id",
            "name": "Saving & Loading Palette",
            "fingerprint": ["cyan", "magenta", "yellow"],
        }] if kind == "palette" else [],
    )

    reconciled = client.post(
        "/api/guides/runtime/resources/reconcile",
        json={"page_id": "owner-page", "session_id": session_id},
        headers=headers,
    )

    assert reconciled.status_code == 200
    assert reconciled.json()["present"] == [{
        **resource,
        "id": "authoritative-id",
        "status": "present",
    }]
