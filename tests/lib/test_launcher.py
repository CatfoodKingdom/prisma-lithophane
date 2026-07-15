from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import Prisma.launcher as launcher


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def test_health_probe_accepts_only_the_prisma_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"ok": True, "app": "prisma-generator"}),
    )
    assert launcher._probe_prisma_generator("http://127.0.0.1:8010") is True

    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"ok": True, "app": "some-other-app"}),
    )
    assert launcher._probe_prisma_generator("http://127.0.0.1:8010") is False


def test_port_reservation_skips_an_occupied_port() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    occupied_port = int(occupied.getsockname()[1])
    try:
        selected_port, listener = launcher._reserve_port("127.0.0.1", occupied_port, attempts=2)
        try:
            assert selected_port == occupied_port + 1
            assert listener.getsockname()[1] == selected_port
        finally:
            listener.close()
    finally:
        occupied.close()


def test_port_reservation_reports_exhaustion() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = int(occupied.getsockname()[1])
    try:
        with pytest.raises(launcher.LauncherError, match=f"from {port} through {port}"):
            launcher._reserve_port("127.0.0.1", port, attempts=1)
    finally:
        occupied.close()


def test_existing_prisma_instance_is_reopened_without_new_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_probe_prisma_generator", lambda _url: True)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        launcher,
        "resolve_runtime_layout",
        lambda **_kwargs: pytest.fail("existing instance must not initialize another runtime"),
    )

    result = launcher.run_generator(
        app_root=tmp_path,
        host="127.0.0.1",
        preferred_port=8010,
        open_browser=True,
    )

    assert result == 0
    assert opened == ["http://127.0.0.1:8010"]


def test_workspace_lock_reopens_instance_that_selected_an_alternate_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alternate = "http://127.0.0.1:8017"
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_probe_prisma_generator", lambda url: url == alternate)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        launcher,
        "resolve_runtime_layout",
        lambda **_kwargs: SimpleNamespace(generator_workspace_root=tmp_path / "Workspace"),
    )
    monkeypatch.setattr(launcher, "prepare_generator_runtime", lambda _layout: None)

    class BusyLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            raise launcher.WorkspaceLockError("busy", owner_url=alternate)

        def release(self):
            pass

    monkeypatch.setattr(launcher, "WorkspaceLock", BusyLock)

    assert launcher.run_generator(
        app_root=tmp_path,
        host="127.0.0.1",
        preferred_port=8010,
        open_browser=True,
    ) == 0
    assert opened == [alternate]


def test_export_stage_recovery_runs_only_after_workspace_lock_is_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    layout = SimpleNamespace(
        generator_workspace_root=tmp_path / "Workspace",
        generator_exports_root=tmp_path / "Exports",
    )
    monkeypatch.setattr(launcher, "_probe_prisma_generator", lambda _url: False)
    monkeypatch.setattr(launcher, "resolve_runtime_layout", lambda **_kwargs: layout)
    monkeypatch.setattr(launcher, "prepare_generator_runtime", lambda _layout: None)

    class RecordingLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            events.append("lock_acquired")

        def release(self):
            events.append("lock_released")

    class StopAfterRecovery(RuntimeError):
        pass

    def stop_after_recovery(path):
        assert events == ["lock_acquired"]
        assert path == layout.generator_exports_root
        events.append("recovery")
        raise StopAfterRecovery

    monkeypatch.setattr(launcher, "WorkspaceLock", RecordingLock)
    monkeypatch.setattr(launcher, "reconcile_interrupted_export_stages", stop_after_recovery)

    with pytest.raises(StopAfterRecovery):
        launcher.run_generator(
            app_root=tmp_path,
            host="127.0.0.1",
            preferred_port=8010,
            open_browser=False,
        )

    assert events == ["lock_acquired", "recovery", "lock_released"]


def test_frozen_server_load_preloads_top_level_shared_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    expected_server = object()

    def fake_import(name: str):
        imported.append(name)
        return expected_server if name == "Prisma.generator.server" else object()

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.importlib, "import_module", fake_import)

    assert launcher._load_generator_server() is expected_server
    assert imported == [*launcher._FROZEN_SHARED_MODULES, "Prisma.generator.server"]


def test_frozen_launcher_disables_inherited_path_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(launcher, "_probe_prisma_generator", lambda _url: False)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)

    def stop_after_resolution(**kwargs):
        captured.update(kwargs)
        raise launcher.RuntimeLayoutError("stop after resolution")

    monkeypatch.setattr(launcher, "resolve_runtime_layout", stop_after_resolution)

    with pytest.raises(launcher.LauncherError, match="stop after resolution"):
        launcher.run_generator(
            app_root=tmp_path,
            host="127.0.0.1",
            preferred_port=8010,
            open_browser=False,
        )

    assert captured["allow_environment_overrides"] is False


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_launcher_refuses_nonlocal_network_binding(tmp_path: Path, host: str) -> None:
    with pytest.raises(launcher.LauncherError, match="only permits the local host"):
        launcher.run_generator(
            app_root=tmp_path,
            host=host,
            preferred_port=8010,
            open_browser=False,
        )


def test_main_returns_friendly_failure_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(launcher, "run_generator", lambda **_kwargs: (_ for _ in ()).throw(launcher.LauncherError("bad library")))

    assert launcher.main(["--no-browser"]) == 1
    captured = capsys.readouterr()
    assert "Prisma could not start" in captured.err
    assert "bad library" in captured.err


def test_main_treats_ctrl_c_as_normal_shutdown(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(launcher, "run_generator", lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert launcher.main(["--no-browser"]) == 0
    captured = capsys.readouterr()
    assert "Prisma stopped." in captured.out
    assert captured.err == ""


def test_source_restart_spawns_launcher_with_original_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.delattr(launcher.sys, "frozen", raising=False)
    monkeypatch.setattr(launcher.sys, "executable", "C:/Python/python.exe")
    monkeypatch.setattr(launcher.sys, "argv", ["C:/Prisma/Prisma/launcher.py", "--port", "8017"])
    monkeypatch.setenv("PRISMA_MODEL_LIBRARY_ROOT", "C:/old/recovery")
    monkeypatch.setenv("UNRELATED_ENVIRONMENT", "keep-me")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, *, env: called.append((command, env)))

    launcher._spawn_restarted_process()

    assert called[0][0] == ["C:/Python/python.exe", "C:/Prisma/Prisma/launcher.py", "--port", "8017"]
    assert "PRISMA_MODEL_LIBRARY_ROOT" not in called[0][1]
    assert called[0][1]["UNRELATED_ENVIRONMENT"] == "keep-me"


def test_frozen_restart_spawns_application_without_duplicate_exe_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", "C:/Prisma/Prisma Generator.exe")
    monkeypatch.setattr(launcher.sys, "argv", ["C:/Prisma/Prisma Generator.exe", "--port", "8017"])
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, *, env: called.append((command, env)))

    launcher._spawn_restarted_process()

    assert called[0][0] == ["C:/Prisma/Prisma Generator.exe", "--port", "8017"]
