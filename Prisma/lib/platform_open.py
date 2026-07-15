"""Open trusted local folders with the host platform's file manager."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _platform_family() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _is_wsl() -> bool:
    if _platform_family() != "linux":
        return False
    if str(os.environ.get("WSL_DISTRO_NAME") or "").strip():
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()


def _spawn(command: Sequence[str]) -> None:
    try:
        subprocess.Popen(
            [str(part) for part in command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise OSError(f"could not start the file manager: {exc}") from exc


def _wsl_windows_path(path: Path) -> str:
    converter = shutil.which("wslpath")
    if not converter:
        raise OSError("WSL path conversion is unavailable")
    try:
        completed = subprocess.run(
            [converter, "-w", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not convert the WSL folder path: {exc}") from exc
    converted = completed.stdout.strip()
    if not converted:
        raise OSError("WSL returned an empty Windows folder path")
    return converted


def open_folder_in_file_manager(path: str | Path) -> None:
    """Open an existing trusted folder without invoking a command shell."""

    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise OSError(f"folder is missing: {folder}")

    family = _platform_family()
    if family == "windows":
        startfile = getattr(os, "startfile", None)
        if not callable(startfile):
            raise OSError("Windows folder opening is unavailable")
        try:
            startfile(str(folder))
        except OSError as exc:
            raise OSError(f"could not start the file manager: {exc}") from exc
        return

    if family == "macos":
        opener = shutil.which("open")
        if not opener:
            raise OSError(f"macOS file-manager launcher is unavailable; open this folder manually: {folder}")
        _spawn([opener, str(folder)])
        return

    opener = shutil.which("xdg-open")
    if opener:
        _spawn([opener, str(folder)])
        return

    if _is_wsl():
        explorer = shutil.which("explorer.exe")
        if explorer:
            _spawn([explorer, _wsl_windows_path(folder)])
            return

    raise OSError(f"no supported file-manager launcher was found; open this folder manually: {folder}")


__all__ = ["open_folder_in_file_manager"]
