from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from Prisma.generator import server


class _Store:
    def __init__(self) -> None:
        self.installed_bytes: bytes | None = None
        self.activated: str | None = None
        self.removed: str | None = None
        self.selected: str | None = None

    def list(self) -> dict:
        return {
            "active_library_id": self.selected,
            "active_state_error": None,
            "libraries": [
                {
                    "valid": True,
                    "library_id": "library-a",
                    "library_name": "Colors A",
                    "active": self.selected == "library-a",
                },
                {
                    "valid": False,
                    "library_id": "broken" if self.selected == "broken" else None,
                    "directory_name": "broken",
                    "active": self.selected == "broken",
                    "error": "corrupt",
                },
                {
                    "valid": True,
                    "library_id": "library-new",
                    "library_name": "New Colors",
                    "active": self.selected == "library-new",
                },
            ],
        }

    def install(self, path) -> dict:
        self.installed_bytes = path.read_bytes()
        return {"library_id": "library-new", "library_name": "New Colors"}

    def activate(self, library_id: str) -> dict:
        self.activated = library_id
        self.selected = library_id
        return {"library_id": library_id, "library_name": "Selected"}

    def remove(self, library_id: str) -> None:
        self.removed = library_id


@pytest.fixture
def library_api(monkeypatch: pytest.MonkeyPatch):
    fake = _Store()
    server._RESTART_REQUESTED.clear()
    server.configure_restart_callback(None)
    monkeypatch.setattr(server, "_MODEL_LIBRARY_STORE", fake)
    monkeypatch.setattr(server, "_ACTIVE_MODEL_LIBRARY_ID", "library-a")
    for key in ("solve", "export", "suggest"):
        monkeypatch.setitem(server.session[key], "status", "idle")
    assert not server._MODEL_LIBRARY_OPERATION_LOCK.locked()
    yield fake
    if server._MODEL_LIBRARY_OPERATION_LOCK.locked():
        server._MODEL_LIBRARY_OPERATION_LOCK.release()
    server._RESTART_REQUESTED.clear()
    server.configure_restart_callback(None)


def test_list_distinguishes_runtime_active_from_next_launch_selection(library_api: _Store) -> None:
    library_api.selected = "library-new"

    status = server.list_model_libraries()

    assert status["active_library_id"] == "library-new"
    assert status["runtime_active_library_id"] == "library-a"
    assert status["restart_required"] is True
    assert status["libraries"][0]["runtime_active"] is True
    assert status["libraries"][0]["selected_for_next_launch"] is False
    assert status["libraries"][1]["valid"] is False


def test_install_stages_upload_then_always_removes_temporary_file(library_api: _Store) -> None:
    upload = UploadFile(filename="colors.zip", file=BytesIO(b"zip payload"))

    response = server.install_model_library(upload)

    assert library_api.installed_bytes == b"zip payload"
    assert response["installed"]["library_id"] == "library-new"
    assert not server._MODEL_LIBRARY_OPERATION_LOCK.locked()


def test_activate_records_next_launch_and_requires_restart(library_api: _Store) -> None:
    response = server.activate_model_library(server.ModelLibraryIdPayload(library_id="library-new"))

    assert library_api.activated == "library-new"
    assert response["restart_required"] is True
    assert response["status"]["active_library_id"] == "library-new"


def test_remove_blocks_library_loaded_by_running_process(library_api: _Store) -> None:
    with pytest.raises(HTTPException) as excinfo:
        server.remove_model_library(server.ModelLibraryIdPayload(library_id="library-a"))

    assert excinfo.value.status_code == 400
    assert "currently loaded" in str(excinfo.value.detail)
    assert library_api.removed is None


def test_library_mutation_is_blocked_during_solve(library_api: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server.session["solve"], "status", "running")

    with pytest.raises(HTTPException) as excinfo:
        server.activate_model_library(server.ModelLibraryIdPayload(library_id="library-new"))

    assert excinfo.value.status_code == 409
    assert "solve job" in str(excinfo.value.detail)
    assert not server._MODEL_LIBRARY_OPERATION_LOCK.locked()


def test_model_job_reservation_is_blocked_during_library_mutation(library_api: _Store) -> None:
    server._MODEL_LIBRARY_OPERATION_LOCK.acquire()
    try:
        with pytest.raises(HTTPException) as excinfo:
            server._reserve_model_job(
                "solve",
                already_running="Solve already running",
                state={"status": "running"},
            )
        assert excinfo.value.status_code == 409
        assert "model-library operation" in str(excinfo.value.detail)
        assert server.session["solve"]["status"] == "idle"
    finally:
        server._MODEL_LIBRARY_OPERATION_LOCK.release()


def test_open_folder_uses_only_configured_model_library_root(
    library_api: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(server, "_open_model_libraries_folder", lambda: opened.append(True))

    assert server.open_model_libraries_folder() == {"opened": True}
    assert opened == [True]


def test_restart_requires_launcher_callback(library_api: _Store) -> None:
    library_api.selected = "library-new"

    with pytest.raises(HTTPException) as excinfo:
        server.restart_prisma()

    assert excinfo.value.status_code == 503
    assert "close and reopen" in str(excinfo.value.detail)


def test_restart_acknowledges_then_invokes_launcher_callback(
    library_api: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_api.selected = "library-new"
    callbacks: list[str] = []
    server.configure_restart_callback(lambda: callbacks.append("restart"))

    class ImmediateTimer:
        daemon = False

        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr(server.threading, "Timer", ImmediateTimer)

    assert server.restart_prisma() == {"restarting": True}
    assert server._RESTART_REQUESTED.is_set()
    assert callbacks == ["restart"]

    with pytest.raises(HTTPException, match="restarting"):
        server._reserve_model_job("solve", already_running="busy", state={"status": "running"})


def test_restart_is_rejected_when_runtime_already_matches_selection(library_api: _Store) -> None:
    library_api.selected = "library-a"
    server.configure_restart_callback(lambda: pytest.fail("restart callback must not run"))

    with pytest.raises(HTTPException) as excinfo:
        server.restart_prisma()

    assert excinfo.value.status_code == 409
    assert "already using" in str(excinfo.value.detail)
    assert not server._RESTART_REQUESTED.is_set()


def test_restart_is_rejected_while_model_job_is_running(
    library_api: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_api.selected = "library-new"
    monkeypatch.setitem(server.session["export"], "status", "running")
    server.configure_restart_callback(lambda: pytest.fail("restart callback must not run"))

    with pytest.raises(HTTPException) as excinfo:
        server.restart_prisma()

    assert excinfo.value.status_code == 409
    assert "export job" in str(excinfo.value.detail)
    assert not server._RESTART_REQUESTED.is_set()


def test_restart_refuses_invalid_selected_library(library_api: _Store) -> None:
    library_api.selected = "broken"
    server.configure_restart_callback(lambda: pytest.fail("restart callback must not run"))

    with pytest.raises(HTTPException) as excinfo:
        server.restart_prisma()

    assert excinfo.value.status_code == 409
    assert "select a valid library" in str(excinfo.value.detail)
    assert not server._RESTART_REQUESTED.is_set()


def test_open_folder_failure_is_actionable(
    library_api: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_open_model_libraries_folder",
        lambda: (_ for _ in ()).throw(OSError("Explorer unavailable")),
    )

    with pytest.raises(HTTPException) as excinfo:
        server.open_model_libraries_folder()

    assert excinfo.value.status_code == 500
    assert "Explorer unavailable" in str(excinfo.value.detail)
