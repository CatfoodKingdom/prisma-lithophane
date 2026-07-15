from __future__ import annotations

from pathlib import Path

import pytest

import Prisma.lib.platform_open as platform_open


def test_windows_uses_startfile_without_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "folder with spaces"
    folder.mkdir()
    opened: list[str] = []
    monkeypatch.setattr(platform_open, "_platform_family", lambda: "windows")
    monkeypatch.setattr(platform_open.os, "startfile", lambda value: opened.append(value), raising=False)

    platform_open.open_folder_in_file_manager(folder)

    assert opened == [str(folder.resolve())]


@pytest.mark.parametrize(
    ("family", "launcher"),
    [("macos", "/usr/bin/open"), ("linux", "/usr/bin/xdg-open")],
)
def test_posix_desktops_use_argument_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    launcher: str,
) -> None:
    folder = tmp_path / "folder;not-shell-syntax"
    folder.mkdir()
    launched: list[list[str]] = []
    monkeypatch.setattr(platform_open, "_platform_family", lambda: family)
    monkeypatch.setattr(platform_open.shutil, "which", lambda name: launcher if name in {"open", "xdg-open"} else None)
    monkeypatch.setattr(platform_open, "_spawn", lambda command: launched.append(list(command)))

    platform_open.open_folder_in_file_manager(folder)

    assert launched == [[launcher, str(folder.resolve())]]


def test_wsl_falls_back_to_explorer_with_a_converted_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Linux folder"
    folder.mkdir()
    launched: list[list[str]] = []
    monkeypatch.setattr(platform_open, "_platform_family", lambda: "linux")
    monkeypatch.setattr(platform_open, "_is_wsl", lambda: True)
    monkeypatch.setattr(
        platform_open.shutil,
        "which",
        lambda name: "/mnt/c/Windows/explorer.exe" if name == "explorer.exe" else None,
    )
    monkeypatch.setattr(platform_open, "_wsl_windows_path", lambda _path: r"\\wsl.localhost\Ubuntu\tmp\Linux folder")
    monkeypatch.setattr(platform_open, "_spawn", lambda command: launched.append(list(command)))

    platform_open.open_folder_in_file_manager(folder)

    assert launched == [["/mnt/c/Windows/explorer.exe", r"\\wsl.localhost\Ubuntu\tmp\Linux folder"]]


def test_headless_linux_reports_the_manual_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setattr(platform_open, "_platform_family", lambda: "linux")
    monkeypatch.setattr(platform_open, "_is_wsl", lambda: False)
    monkeypatch.setattr(platform_open.shutil, "which", lambda _name: None)

    with pytest.raises(OSError, match="open this folder manually"):
        platform_open.open_folder_in_file_manager(folder)


def test_missing_folder_is_rejected_before_launch(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="folder is missing"):
        platform_open.open_folder_in_file_manager(tmp_path / "missing")
