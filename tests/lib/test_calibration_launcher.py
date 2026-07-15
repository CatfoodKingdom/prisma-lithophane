from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import Prisma.calibration_launcher as launcher


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def test_health_probe_accepts_only_prisma_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"ok": True, "app": "prisma-calibration"}),
    )
    assert launcher._probe_prisma_calibration("http://127.0.0.1:8016") is True

    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"ok": True, "app": "prisma-generator"}),
    )
    assert launcher._probe_prisma_calibration("http://127.0.0.1:8016") is False


def test_health_probe_can_reject_a_different_port_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({
            "ok": True,
            "app": "prisma-calibration",
            "app_root": str(tmp_path),
        }),
    )
    assert launcher._probe_prisma_calibration(
        "http://127.0.0.1:8016",
        expected_app_root=tmp_path,
    ) is True
    assert launcher._probe_prisma_calibration(
        "http://127.0.0.1:8016",
        expected_app_root=tmp_path / "other",
    ) is False


def test_port_reservation_skips_an_occupied_port() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = int(occupied.getsockname()[1])
    try:
        selected, listener = launcher._reserve_port("127.0.0.1", port, attempts=2)
        try:
            assert selected == port + 1
        finally:
            listener.close()
    finally:
        occupied.close()


def test_existing_instance_reopens_without_runtime_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_probe_prisma_calibration", lambda _url, **_kwargs: True)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        launcher,
        "resolve_runtime_layout",
        lambda **_kwargs: pytest.fail("existing instance must not initialize another runtime"),
    )

    assert launcher.run_calibration(
        app_root=tmp_path,
        host="127.0.0.1",
        preferred_port=8016,
        open_browser=True,
    ) == 0
    assert opened == ["http://127.0.0.1:8016"]


def test_workspace_lock_reopens_instance_on_an_alternate_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alternate = "http://127.0.0.1:8021"
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_probe_prisma_calibration", lambda url, **_kwargs: url == alternate)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        launcher,
        "resolve_runtime_layout",
        lambda **_kwargs: SimpleNamespace(calibration_workspace_root=tmp_path / "Workspace"),
    )
    monkeypatch.setattr(launcher, "prepare_calibration_runtime", lambda _layout: None)

    class BusyLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            raise launcher.WorkspaceLockError("busy", owner_url=alternate)

        def release(self):
            pass

    monkeypatch.setattr(launcher, "WorkspaceLock", BusyLock)

    assert launcher.run_calibration(
        app_root=tmp_path,
        host="127.0.0.1",
        preferred_port=8016,
        open_browser=True,
    ) == 0
    assert opened == [alternate]


def test_frozen_launcher_disables_inherited_path_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(launcher, "_probe_prisma_calibration", lambda _url, **_kwargs: False)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)

    def stop_after_resolution(**kwargs):
        captured.update(kwargs)
        raise launcher.RuntimeLayoutError("stop after resolution")

    monkeypatch.setattr(launcher, "resolve_runtime_layout", stop_after_resolution)
    with pytest.raises(launcher.CalibrationLauncherError, match="stop after resolution"):
        launcher.run_calibration(
            app_root=tmp_path,
            host="127.0.0.1",
            preferred_port=8016,
            open_browser=False,
        )

    assert captured["allow_environment_overrides"] is False


def test_frozen_server_load_preloads_top_level_shared_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    expected_server = object()

    def fake_import(name: str):
        imported.append(name)
        return expected_server if name == "Prisma.calibration.server" else object()

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.importlib, "import_module", fake_import)

    assert launcher._load_calibration_server() is expected_server
    assert imported == [*launcher._FROZEN_SHARED_MODULES, "Prisma.calibration.server"]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_launcher_refuses_nonlocal_network_binding(tmp_path: Path, host: str) -> None:
    with pytest.raises(launcher.CalibrationLauncherError, match="only permits the local host"):
        launcher.run_calibration(
            app_root=tmp_path,
            host=host,
            preferred_port=8016,
            open_browser=False,
        )


def test_main_returns_a_friendly_failure_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        launcher,
        "run_calibration",
        lambda **_kwargs: (_ for _ in ()).throw(launcher.CalibrationLauncherError("bad workspace")),
    )

    assert launcher.main(["--no-browser"]) == 1
    captured = capsys.readouterr()
    assert "Prisma Calibration could not start" in captured.err
    assert "bad workspace" in captured.err


def test_main_treats_ctrl_c_as_normal_shutdown(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(launcher, "run_calibration", lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert launcher.main(["--no-browser"]) == 0
    captured = capsys.readouterr()
    assert "Prisma Calibration stopped." in captured.out
    assert captured.err == ""
