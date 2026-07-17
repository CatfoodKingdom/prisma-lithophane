"""Disposable-runtime browser smoke for the Calibration application shell."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
import pytest


pytestmark = pytest.mark.browser

_ROOT = Path(__file__).resolve().parents[3]
_CALIBRATION = _ROOT / "Prisma" / "calibration"


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_calibration_shell_starts_against_disposable_json_runtime(tmp_path: Path):
    data_root = tmp_path / "calibration-data"
    port = _available_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(_CALIBRATION / "server.py"),
            "--backend",
            "json",
            "--data-root",
            str(data_root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Calibration browser-test server exited with {process.returncode}")
            try:
                with urlopen(f"{base_url}/api/system/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Calibration browser-test server did not become healthy")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(base_url, wait_until="networkidle")

            assert page.title() == "Unified Calibration Workbook"
            assert page.locator("#modeSwitch .mode-button").all_inner_texts() == [
                "Logbook",
                "Images",
                "Filaments",
                "Geometries",
                "Modeling",
            ]
            health = page.evaluate("async () => await (await fetch('/api/system/health')).json()")
            assert health["app"] == "prisma-calibration"
            assert health["mode"] == "normal"
            assert page_errors == []
            browser.close()

        assert data_root.resolve().is_relative_to(tmp_path.resolve())
        assert data_root.exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
