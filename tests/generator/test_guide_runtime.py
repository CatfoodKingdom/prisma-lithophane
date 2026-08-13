import json
from pathlib import Path

import pytest

import guide_runtime
from guide_runtime import (
    GuideRuntimeConflict,
    GuideRuntimeCorrupt,
    GuideRuntimeStore,
)


def _snapshot() -> dict:
    return {
        "server": {
            "active_printer_id": "my-printer",
            "printer_setup_state": {"active_nozzle_id": "nozzle-600", "current_width_um": 600},
        },
        "client": {"solve_mode": "batch", "settings": {"loaded": "profile-a"}},
    }


def test_provisional_lease_can_be_declined_without_creating_snapshot(tmp_path: Path) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    lease = store.acquire("page-a")["lease"]

    status = store.release(lease["lease_id"], "page-a")

    assert status["session"] is None
    assert not store.path.exists()


def test_repeated_acquire_from_same_page_reuses_provisional_lease(tmp_path: Path) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")

    first = store.acquire("page-a")["lease"]["lease_id"]
    second = store.acquire("page-a")["lease"]["lease_id"]

    assert second == first


def test_begin_atomically_persists_snapshot_before_workspace_mutation(tmp_path: Path) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    lease_id = store.acquire("page-a")["lease"]["lease_id"]

    record = store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="prisma-generator-basics",
        route_id="full",
        snapshot=_snapshot(),
    )

    assert record["snapshot"] == _snapshot()
    assert json.loads(store.path.read_text(encoding="utf-8"))["snapshot"] == _snapshot()
    with pytest.raises(GuideRuntimeConflict):
        store.acquire("page-b")


def test_owner_checks_epoch_invalidation_and_epoch_persistence(tmp_path: Path) -> None:
    path = tmp_path / "guide_runtime.json"
    store = GuideRuntimeStore(path)
    lease_id = store.acquire("page-a")["lease"]["lease_id"]
    record = store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="prisma-generator-basics",
        route_id="image",
        snapshot=_snapshot(),
    )
    assert store.authorize_mutation(
        page_id="page-a", session_id=record["session_id"], workspace_epoch=0,
    ) == (True, None)
    assert store.authorize_mutation(
        page_id="page-b", session_id=None, workspace_epoch=0,
    )[0] is False
    with pytest.raises(GuideRuntimeConflict):
        store.finalize(record["session_id"], "page-b")

    store.mark_restoring(record["session_id"], "page-a")
    assert store.authorize_mutation(
        page_id="page-a", session_id=record["session_id"], workspace_epoch=0,
    )[0] is False
    assert store.authorize_mutation(
        page_id="page-a",
        session_id=record["session_id"],
        workspace_epoch=0,
        allow_recovery=True,
    ) == (True, None)
    store.mark_restored(record["session_id"], "page-a")
    assert store.finalize(record["session_id"], "page-a") == 1
    assert store.authorize_mutation(
        page_id="page-a", session_id=None, workspace_epoch=0,
    )[0] is False
    assert GuideRuntimeStore(path).status()["workspace_epoch"] == 1


def test_recovery_takeover_waits_for_owner_timeout(tmp_path: Path, monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(guide_runtime.time, "monotonic", lambda: now)
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json", lease_timeout_s=90)
    lease_id = store.acquire("page-a")["lease"]["lease_id"]
    record = store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="prisma-generator-basics",
        route_id="full",
        snapshot=_snapshot(),
    )
    with pytest.raises(GuideRuntimeConflict, match="still active"):
        store.claim_recovery(record["session_id"], "page-b")

    now = 191.0
    recovered = store.claim_recovery(record["session_id"], "page-b")
    assert recovered["owner_page_id"] == "page-b"
    assert recovered["phase"] == "restoring"


def test_corrupt_snapshot_fails_closed_and_abandon_preserves_it(tmp_path: Path) -> None:
    path = tmp_path / "guide_runtime.json"
    path.write_text("{broken", encoding="utf-8")
    store = GuideRuntimeStore(path)

    with pytest.raises(GuideRuntimeCorrupt):
        store.status()
    assert store.authorize_mutation(page_id="page", session_id=None, workspace_epoch=0)[0] is False

    preserved, epoch = store.abandon()
    assert epoch == 1
    assert preserved is not None and preserved.read_text(encoding="utf-8") == "{broken"
    assert not path.exists()


def test_snapshot_with_missing_ownership_sections_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "guide_runtime.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "session_id": "session",
            "guide_id": "guide",
            "route_id": "full",
            "owner_page_id": "page",
            "phase": "active",
            "snapshot": {"server": {}},
            "owned_jobs": {},
        }),
        encoding="utf-8",
    )

    with pytest.raises(GuideRuntimeCorrupt, match="ownership sections"):
        GuideRuntimeStore(path).status()


def test_durable_snapshot_tampering_fails_integrity_check(tmp_path: Path) -> None:
    path = tmp_path / "guide_runtime.json"
    store = GuideRuntimeStore(path)
    lease_id = store.acquire("page-a")["lease"]["lease_id"]
    store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="prisma-generator-basics",
        route_id="full",
        snapshot=_snapshot(),
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["snapshot"]["client"]["solve_mode"] = "single"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(GuideRuntimeCorrupt, match="integrity"):
        store.status()


def test_active_window_count_excludes_requesting_page(tmp_path: Path, monkeypatch) -> None:
    now = 20.0
    monkeypatch.setattr(guide_runtime.time, "monotonic", lambda: now)
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json", lease_timeout_s=90)
    store.note_page("page-a")
    store.note_page("page-b")
    assert store.status(page_id="page-a")["other_active_windows"] == 1
    now = 111.0
    assert store.status(page_id="page-a")["other_active_windows"] == 0


def test_departed_owner_relinquishes_lease_but_preserves_recovery_record(tmp_path: Path) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    lease_id = store.acquire("page-a")["lease"]["lease_id"]
    record = store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="saving-and-loading",
        route_id="full",
        snapshot=_snapshot(),
    )

    departed = store.depart_page("page-a")

    assert departed["lease"] is None
    assert departed["session"]["session_id"] == record["session_id"]
    claimed = store.claim_recovery(record["session_id"], "replacement-page")
    assert claimed["owner_page_id"] == "replacement-page"
    assert claimed["phase"] == "restoring"


def test_owned_resource_journal_enforces_identity_and_transition_order(tmp_path: Path) -> None:
    store = GuideRuntimeStore(tmp_path / "guide_runtime.json")
    lease_id = store.acquire("page-a")["lease"]["lease_id"]
    record = store.begin(
        lease_id=lease_id,
        page_id="page-a",
        guide_id="saving-and-loading",
        route_id="full",
        snapshot=_snapshot(),
    )
    session_id = record["session_id"]
    base = {
        "operation_id": "saving-loading-palette",
        "kind": "palette",
        "id": "palette-card",
        "name": "Saving & Loading Palette",
        "fingerprint": ["cyan", "magenta", "yellow"],
    }

    pending = store.transition_resource(
        session_id, "page-a", {**base, "status": "pending_create"}
    )
    assert pending["status"] == "pending_create"
    assert store.transition_resource(
        session_id, "page-a", {**base, "status": "pending_create"}
    ) == pending
    assert store.transition_resource(
        session_id, "page-a", {**base, "status": "present"}
    )["status"] == "present"

    with pytest.raises(GuideRuntimeConflict, match="name cannot change"):
        store.transition_resource(
            session_id,
            "page-a",
            {**base, "name": "Different", "status": "pending_delete"},
        )
    with pytest.raises(GuideRuntimeConflict, match="invalid guide resource transition"):
        store.transition_resource(
            session_id, "page-a", {**base, "status": "absent"}
        )

    store.transition_resource(
        session_id, "page-a", {**base, "status": "pending_delete"}
    )
    absent = store.transition_resource(
        session_id, "page-a", {**base, "status": "absent"}
    )
    assert absent["status"] == "absent"
    assert store.read()["owned_resources"][base["operation_id"]] == absent
