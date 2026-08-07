import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from guide_state import (
    GuideStateError,
    GuideStateRevisionConflict,
    GuideStateStore,
    default_guide_state,
)
import server


def _state(*, revision: int = 0, welcome_status: str = "declined") -> dict:
    state = default_guide_state()
    state["revision"] = revision
    state["welcome_status"] = welcome_status
    return state


def test_missing_state_reads_defaults_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "config" / "guide_state.json"
    store = GuideStateStore(path)

    assert store.read() == default_guide_state()
    assert not path.exists()


def test_replace_is_atomic_and_revision_guarded(tmp_path: Path) -> None:
    path = tmp_path / "config" / "guide_state.json"
    store = GuideStateStore(path)

    saved = store.replace(_state(), expected_revision=0)

    assert saved["revision"] == 1
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    with pytest.raises(GuideStateRevisionConflict) as conflict:
        store.replace(_state(), expected_revision=0)
    assert conflict.value.actual == 1


def test_payload_revision_must_match_expected_revision(tmp_path: Path) -> None:
    store = GuideStateStore(tmp_path / "guide_state.json")

    with pytest.raises(GuideStateError, match="must match"):
        store.replace(_state(revision=4), expected_revision=0)


def test_replacement_requires_the_complete_current_schema(tmp_path: Path) -> None:
    store = GuideStateStore(tmp_path / "guide_state.json")
    incomplete = _state()
    incomplete.pop("welcome_status")

    with pytest.raises(GuideStateError, match="canonical schema fields"):
        store.replace(incomplete, expected_revision=0)


def test_replacement_rejects_removed_guide_progress_fields(tmp_path: Path) -> None:
    store = GuideStateStore(tmp_path / "guide_state.json")
    state = _state()
    state["active_guide"] = {
        "guide_id": "interface-preview",
        "guide_version": 1,
        "step_id": "workflow-tabs",
    }

    with pytest.raises(GuideStateError, match="canonical schema fields"):
        store.replace(state, expected_revision=0)


def test_malformed_state_is_preserved_before_defaults_are_returned(tmp_path: Path) -> None:
    path = tmp_path / "config" / "guide_state.json"
    path.parent.mkdir()
    path.write_text("{not-json", encoding="utf-8")
    store = GuideStateStore(path)

    assert store.read() == default_guide_state()
    assert not path.exists()
    preserved = list(path.parent.glob("guide_state.corrupt-*.json"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "{not-json"


def test_read_io_failure_is_not_misclassified_as_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "guide_state.json"
    path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_for_state_file(self, *args, **kwargs):
        if self == path:
            raise PermissionError("locked")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_state_file)

    with pytest.raises(GuideStateError, match="could not be read"):
        GuideStateStore(path).read()
    assert path.exists()
    assert not list(tmp_path.glob("guide_state.corrupt-*.json"))


def test_legacy_unversioned_state_migrates_to_first_launch_only(tmp_path: Path) -> None:
    path = tmp_path / "guide_state.json"
    path.write_text(
        json.dumps(
            {
                "welcome_offered": True,
                "active_guide": None,
                "completed_guides": {},
            }
        ),
        encoding="utf-8",
    )

    assert GuideStateStore(path).read() == {
        "schema_version": 2,
        "revision": 0,
        "welcome_status": "declined",
    }


def test_schema_one_discards_obsolete_guide_progress(tmp_path: Path) -> None:
    path = tmp_path / "guide_state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 7,
                "welcome_status": "accepted",
                "active_guide": {
                    "guide_id": "interface-preview",
                    "guide_version": 1,
                    "step_id": "white-point-rescale",
                },
                "completed_guides": {
                    "interface-preview": {
                        "guide_version": 1,
                        "completed_utc": "2026-07-30T12:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert GuideStateStore(path).read() == {
        "schema_version": 2,
        "revision": 7,
        "welcome_status": "accepted",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2.5, "schema_version must be an integer"),
        ("revision", 0.5, "revision must be a non-negative integer"),
    ],
)
def test_schema_and_revision_numbers_are_strict_integers(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    store = GuideStateStore(tmp_path / "guide_state.json")
    state = _state()
    state[field] = value

    with pytest.raises(GuideStateError, match=message):
        store.replace(state, expected_revision=0)


def test_guide_state_api_round_trip_and_conflict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_GUIDE_STATE_STORE",
        GuideStateStore(tmp_path / "config" / "guide_state.json"),
    )
    client = TestClient(server.app)

    initial = client.get("/api/guides/state")
    assert initial.status_code == 200
    assert initial.json() == default_guide_state()

    saved = client.put(
        "/api/guides/state",
        json={"expected_revision": 0, "state": _state()},
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "schema_version": 2,
        "revision": 1,
        "welcome_status": "declined",
    }

    conflict = client.put(
        "/api/guides/state",
        json={"expected_revision": 0, "state": _state()},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 1


def test_guide_state_api_rejects_invalid_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_GUIDE_STATE_STORE",
        GuideStateStore(tmp_path / "guide_state.json"),
    )
    client = TestClient(server.app)
    invalid = _state()
    invalid["welcome_status"] = "surprise"

    response = client.put(
        "/api/guides/state",
        json={"expected_revision": 0, "state": invalid},
    )

    assert response.status_code == 422


def test_guide_state_api_rejects_non_integer_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_GUIDE_STATE_STORE",
        GuideStateStore(tmp_path / "guide_state.json"),
    )
    client = TestClient(server.app)

    response = client.put(
        "/api/guides/state",
        json={"expected_revision": 0.5, "state": _state()},
    )

    assert response.status_code == 422


def test_old_basics_prepare_endpoint_was_removed() -> None:
    response = TestClient(server.app).post(
        "/api/guides/basics/prepare",
        json={"restore_tutorial_printer": True},
    )

    assert response.status_code == 405


def test_protected_guide_asset_cannot_mount_without_a_durable_session() -> None:
    response = TestClient(server.app).post(
        "/api/guides/runtime/assets/bubba-blanket/mount",
        json={"session_id": "missing", "page_id": "page-a"},
    )

    assert response.status_code == 409
