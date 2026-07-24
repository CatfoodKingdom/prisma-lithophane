"""Behavioral characterization for Calibration's modular frontend."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import Page, Route, expect, sync_playwright
import pytest


pytestmark = pytest.mark.browser

_ROOT = Path(__file__).resolve().parents[3]
_CALIBRATION = _ROOT / "Prisma" / "calibration"


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def calibration_url(tmp_path_factory: pytest.TempPathFactory):
    data_root = tmp_path_factory.mktemp("calibration-browser-data")
    port = _available_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(_CALIBRATION / "server.py"),
            "--backend", "json",
            "--data-root", str(data_root),
            "--host", "127.0.0.1",
            "--port", str(port),
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
        yield base_url
        assert data_root.exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def page(calibration_url: str):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(calibration_url, wait_until="networkidle")
        yield page
        assert errors == []
        browser.close()


def _json(route: Route, payload: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def test_empty_libraries_navigation_and_responsive_geometry(page: Page):
    assert page.get_by_role("tab").all_inner_texts()[:5] == [
        "Logbook", "Images", "Filaments", "Geometries", "Modeling",
    ]
    assert page.locator(".management-library-table th").all_inner_texts() == [
        "ID ↓", "Strip", "Filament", "Brand", "Image", "Blank", "Status",
    ]
    assert "No samples yet. Use + New Samples" in page.locator(".management-library-table").inner_text()

    page.get_by_role("tab", name="Filaments").click()
    assert "No filaments yet. Use + New Filament" in page.locator(".management-library-table").inner_text()
    page.get_by_role("tab", name="Geometries").click()
    assert "No sample geometries yet. Use + New Sample Geometry" in page.locator(".management-library-table").inner_text()

    page.get_by_role("button", name="New Sample Geometry").click()
    drawer = page.locator("#stepBuilderDrawer")
    assert drawer.get_by_role("heading", name="New Sample Geometry").is_visible()
    assert drawer.get_by_role("button", name="Generate").is_visible()
    assert drawer.locator("#stepStartValue").is_visible()

    page.set_viewport_size({"width": 820, "height": 900})
    assert drawer.bounding_box()["width"] <= 820
    assert page.locator("body").evaluate("element => element.scrollWidth <= element.clientWidth")
    page.keyboard.press("Escape")
    assert not drawer.is_visible()


def test_image_inbox_and_csv_assignment_guidance(page: Page):
    page.get_by_role("tab", name="Images").click()
    assert page.get_by_role("tab", name="Assign Images").get_attribute("aria-selected") == "true"
    assert page.get_by_role("button", name="Open Inbox Folder").is_visible()
    assert page.get_by_role("button", name="Import from Inbox").is_visible()
    assert page.get_by_role("button", name="Clean Up Unused").is_visible()
    assert page.locator("#importInboxChip").count() == 0
    assert "No images found in inbox" in page.locator("#importImageGrid").inner_text()

    page.get_by_role("button", name="CSV Bulk Assignment").click()
    dialog = page.get_by_role("dialog", name="CSV Bulk Assignment")
    guidance = dialog.inner_text()
    assert "Place every sample image and blank image referenced by the CSV in the Calibration Inbox folder" in guidance
    assert "Import from Inbox" in guidance
    assert "exact filenames" in guidance
    assert dialog.get_by_role("button", name="Validate CSV").is_disabled()
    assert dialog.locator("#csvAssignmentCommit").is_disabled()
    page.keyboard.press("Escape")
    assert not dialog.is_visible()


def test_publication_dialog_required_fields_and_busy_close_guard(page: Page):
    readiness = {
        "ready": True,
        "components": {
            key: {"label": key, "status": "ready", "reason": ""}
            for key in ("legacy_spline", "photo_stack_v2", "camera_transform", "filament_catalog")
        },
        "blocking_reasons": [],
    }
    page.route("**/api/models/publication/readiness", lambda route: _json(route, readiness))
    # Hold the request at the browser network layer. The application injects
    # and retains its fetch transport during bootstrap, so replacing
    # window.fetch after page load would not reliably exercise the busy state.
    page.route("**/api/models/publication/install", lambda route: None)
    page.get_by_role("button", name="Publish Models").click()
    dialog = page.get_by_role("dialog", name="Publish Models")
    expect(dialog.get_by_text("Ready to publish")).to_be_visible()
    for field in ("Library name", "Version", "Publisher or author", "Description", "Release notes"):
        assert dialog.get_by_text(field, exact=False).count() >= 1
    assert dialog.get_by_role("button", name="Publish to Generator").is_disabled()

    dialog.locator("#modelPublicationName").fill("Browser Characterization")
    dialog.locator("#modelPublicationVersion").fill("1.0")
    dialog.locator("#modelPublicationPublisher").fill("Prisma")
    publish = dialog.get_by_role("button", name="Publish to Generator")
    assert publish.is_enabled()
    publish.click()
    assert dialog.get_by_text("Keep this window open until publication finishes.").is_visible()
    assert dialog.locator("#modelPublicationClose").is_disabled()
    page.keyboard.press("Escape")
    assert dialog.is_visible()


def test_maintenance_cancellation_is_authoritative_and_non_error(page: Page):
    cancelled = {"value": False}

    def maintenance_routes(route: Route) -> None:
        url = route.request.url
        if url.endswith("/preflight"):
            _json(route, {"preflight": {"enabled": True, "summary": {}}, "preflight_token": "token"})
        elif url.endswith("/jobs"):
            _json(route, {"job_id": "job-1", "status": "running", "cancellable": True, "cancel_available": True})
        elif url.endswith("/jobs/job-1/cancel"):
            cancelled["value"] = True
            _json(route, {"job_id": "job-1", "status": "cancelled", "cancellable": True, "cancel_available": False, "cancel_requested": True})
        elif url.endswith("/jobs/job-1"):
            status = "cancelled" if cancelled["value"] else "running"
            _json(route, {"job_id": "job-1", "status": status, "cancellable": True, "cancel_available": not cancelled["value"]})
        else:
            route.continue_()

    page.route("**/api/maintenance/**", maintenance_routes)
    page.get_by_role("button", name="Maintenance").click()
    palette = page.get_by_role("dialog", name="Maintenance")
    palette.get_by_role("button", name="Audit Missing Artifacts", exact=False).click()
    palette.get_by_role("button", name="Start Workflow").click()
    workflow = page.get_by_role("dialog", name="Audit Missing Artifacts")
    assert "Cancelable between items" in workflow.inner_text()
    workflow.get_by_role("button", name="Run Preflight").click()
    workflow.get_by_role("button", name="Run Audit", exact=True).click()
    workflow.get_by_role("button", name="Cancel", exact=True).click()
    expect(workflow.locator("#maintenanceWorkflowClose")).to_be_enabled(timeout=5_000)
    assert workflow.locator(".backup-restore-message.is-error").count() == 0
