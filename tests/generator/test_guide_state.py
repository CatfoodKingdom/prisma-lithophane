import json
from pathlib import Path
from types import SimpleNamespace

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


def test_basics_tutorial_image_never_overwrites_a_conflicting_user_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "bundled.jpg"
    source.write_bytes(b"bundled-tutorial-photo")
    images = tmp_path / "Images"
    images.mkdir()
    original = images / server._BASICS_TUTORIAL_IMAGE_NAME
    original.write_bytes(b"user-photo")

    class FakeSourceImages:
        @staticmethod
        def prepare(path: Path):
            return SimpleNamespace(
                display_name=path.name,
                width=90,
                height=120,
            )

    monkeypatch.setattr(server, "_BASICS_TUTORIAL_IMAGE_SOURCE", source)
    monkeypatch.setattr(server, "_IMAGES_DIR", images)
    monkeypatch.setattr(server, "_SOURCE_IMAGES", FakeSourceImages())

    prepared = server._materialize_basics_tutorial_image()

    assert original.read_bytes() == b"user-photo"
    assert prepared.display_name == "Prisma Tutorial - Bubba Blanket 2.jpg"
    assert (images / prepared.display_name).read_bytes() == source.read_bytes()
    assert not list(images.glob(".*.tmp"))


def test_basics_prepare_reports_and_restores_tutorial_printer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tutorial_path = tmp_path / "Prisma Tutorial - Bubba Blanket.jpg"
    tutorial_path.write_bytes(b"tutorial-image-bytes")
    tutorial_image = SimpleNamespace(
        display_name="Prisma Tutorial - Bubba Blanket.jpg",
        width=1600,
        height=1200,
        original_path=tutorial_path,
        source_format="jpeg",
        normalized=False,
    )
    printers = {
        "printers": [server.deepcopy(server._BAMBU_X1C_PRINTER_PROFILE)],
        "active_printer_id": "bambu-x1c",
        "active_nozzle_size": 0.2,
    }
    restored = []
    monkeypatch.setattr(server, "_load_printers", lambda: server.deepcopy(printers))
    monkeypatch.setattr(
        server,
        "_restore_tutorial_printer",
        lambda data: (
            restored.append(True)
            or {
                **server.deepcopy(data),
                "printers": [
                    *server.deepcopy(data["printers"]),
                    server.deepcopy(server._TUTORIAL_PRINTER_PROFILE),
                ],
            }
        ),
    )
    monkeypatch.setattr(
        server,
        "_materialize_basics_tutorial_image",
        lambda: tutorial_image,
    )
    client = TestClient(server.app)

    missing = client.post(
        "/api/guides/basics/prepare",
        json={"restore_tutorial_printer": False},
    )
    repaired = client.post(
        "/api/guides/basics/prepare",
        json={"restore_tutorial_printer": True},
    )

    assert missing.status_code == 200
    assert missing.json()["tutorial_printer"]["status"] == "missing"
    assert repaired.status_code == 200
    assert repaired.json()["tutorial_printer"]["status"] == "ready"
    assert repaired.json()["tutorial_image"]["filename"] == tutorial_image.display_name
    assert repaired.json()["tutorial_image"]["size_kb"] == round(
        tutorial_path.stat().st_size / 1024,
        1,
    )
    assert repaired.json()["tutorial_image"]["source_format"] == "jpeg"
    assert repaired.json()["tutorial_image"]["normalized"] is False
    assert restored == [True]


def test_basics_prepare_skips_unrequested_tutorial_inputs(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("unrequested tutorial preparation ran")

    monkeypatch.setattr(server, "_load_printers", unexpected)
    monkeypatch.setattr(server, "_restore_tutorial_printer", unexpected)
    monkeypatch.setattr(server, "_materialize_basics_tutorial_image", unexpected)
    client = TestClient(server.app)

    response = client.post(
        "/api/guides/basics/prepare",
        json={
            "restore_tutorial_printer": True,
            "include_tutorial_printer": False,
            "include_tutorial_image": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tutorial_image": None,
        "tutorial_printer": None,
    }
