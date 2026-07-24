from __future__ import annotations

import base64
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from PIL import Image
from playwright.sync_api import Browser, Page, Route, sync_playwright
import pytest


pytestmark = pytest.mark.browser

_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = _ROOT / "Prisma" / "generator"
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGP8z8AARAwMjDAGCjAAADYBAQGMY2dAAAAAAElFTkSuQmCC"
)


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def generator_url() -> str:
    port = _available_port()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(_GENERATOR), str(_ROOT / "Prisma"), str(_ROOT), environment.get("PYTHONPATH", "")]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=_GENERATOR,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Generator browser-test server exited with {process.returncode}")
        try:
            with urlopen(f"{base_url}/api/system/health", timeout=1) as response:
                if response.status == 200:
                    break
        except (OSError, URLError):
            time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError("Generator browser-test server did not become healthy")

    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Browser, generator_url: str) -> Page:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    instance = context.new_page()
    instance.set_default_timeout(10_000)
    page_errors: list[str] = []
    instance.on("pageerror", lambda error: page_errors.append(getattr(error, "stack", str(error))))
    instance.goto(generator_url, wait_until="domcontentloaded")
    instance.locator("#dataSourceBadge.connected").wait_for(state="visible")
    recovery_modal = instance.locator("#modelLibrariesModal")
    if recovery_modal.get_attribute("aria-hidden") == "false":
        instance.locator("#modelLibrariesCloseBtn").click()
        recovery_modal.wait_for(state="hidden")
    try:
        yield instance
        assert page_errors == []
    finally:
        context.close()


def _solve_result(card_id: str) -> dict:
    return {
        "card_id": card_id,
        "diagnostic_palette_version": "inferno_v1",
        "mean_de": 1.2,
        "source_rms_de": 1.3,
        "max_de": 2.4,
        "de_scale_max": 3.0,
        "n_oog": 0,
        "total_pixels": 16,
        "coverage_pct": 100.0,
        "image_w": 4,
        "image_h": 4,
        "image_domain_width_mm": 20.0,
        "image_domain_height_mm": 20.0,
        "max_height": 1.0,
        "predicted_url": "/api/run-cache/files/browser-predicted.png",
        "predicted_appearance_url": "/api/run-cache/files/browser-appearance.png",
        "predicted_color_only_appearance_url": "/api/run-cache/files/browser-color.png",
        "source_url": "/api/run-cache/files/browser-source.png",
        "palette_fit_url": "/api/run-cache/files/browser-palette-fit.png",
        "de_map_url": "/api/run-cache/files/browser-de.png",
        "de_map_perceptual_url": "/api/run-cache/files/browser-de-perceptual.png",
        "de_map_maxset_url": "/api/run-cache/files/browser-de-max.png",
        "de_raw_url": "/api/run-cache/files/browser-de-raw.png",
        "cap_map_url": "/api/run-cache/files/browser-cap.png",
        "boundary_cap_map_url": "/api/run-cache/files/browser-boundary.png",
        "detail_cap_map_url": "/api/run-cache/files/browser-detail.png",
        "color_ceiling_url": "/api/run-cache/files/browser-ceiling.png",
        "total_surface_url": "/api/run-cache/files/browser-surface.png",
        "filament_maps": [],
        "filament_bin_urls": {},
        "debug_map_urls": {},
        "explorer_stack_table": [],
        "color_recipe_breakdown_summary": {},
    }


def _fulfill_png(route: Route) -> None:
    route.fulfill(status=200, content_type="image/png", body=_TINY_PNG)


def _close_recovery_modal(page: Page) -> None:
    modal = page.locator("#modelLibrariesModal")
    if modal.get_attribute("aria-hidden") == "false":
        page.locator("#modelLibrariesCloseBtn").click()
        modal.wait_for(state="hidden")


def _install_saved_run_routes(page: Page, *, recipe: bool = False) -> None:
    result = _solve_result("browser-loaded-card")
    if recipe:
        result.update(
            {
                "color_recipe_breakdown_cookbook_url": "/browser-cookbook.json",
                "color_recipe_breakdown_summary": {"color_recipe_count": 2},
            }
        )
        page.route(
            "**/browser-cookbook.json",
            lambda route: route.fulfill(
                json={
                    "families": [],
                    "specific_recipes": [],
                }
            ),
        )
    page.route("**/api/run-cache/files/**", _fulfill_png)
    page.route("**/api/runs/saved/*/preview", _fulfill_png)
    page.route(
        "**/api/runs/saved",
        lambda route: route.fulfill(
            json=[
                {
                    "save_id": "browser-save",
                    "tier": "saved",
                    "label": "Browser saved run",
                    "source_image_name": "browser-source.png",
                    "saved_at": "2026-07-16T12:00:00Z",
                }
            ]
        ),
    )
    page.route(
        "**/api/runs/load",
        lambda route: route.fulfill(
            json={
                "card_id": "browser-loaded-card",
                "label": "Browser saved run",
                "palette": ["bambu-basic-cyan"],
                "config": {
                    "palette": ["bambu-basic-cyan"],
                    "white_base": "panchroma-matte-cotton-white",
                    "white_cap": "panchroma-matte-cotton-white",
                    "base_filament": "panchroma-matte-cotton-white",
                    "cap_filament": "panchroma-matte-cotton-white",
                    "layer_height": 0.08,
                    "d_wb": 0.2,
                    "t_max": 2.0,
                    "image_sample_pitch_mm": 0.4,
                    "solver_fine_pitch_mm": 0.4,
                },
                "result": result,
            }
        ),
    )


def _load_saved_run(page: Page, *, recipe: bool = False) -> None:
    _install_saved_run_routes(page, recipe=recipe)
    page.locator('#tabSwitch .mode-button[data-tab="solve"]').click()
    page.locator("#savedRunsBtn").click()
    page.locator(".saved-run-row").wait_for(state="visible")
    page.locator("#savedRunLoadBtn").click()
    page.locator('.solve-run-card[data-run-id="browser-loaded-card"]').wait_for(state="visible")


def test_settings_persist_through_the_real_session_api(page: Page):
    page.locator("#settingsDrawerBtn").click()
    page.locator("#settingsDrawer").wait_for(state="visible")
    solve_pitch = page.locator("#cfgSolvePitch")

    with page.expect_response(
        lambda response: response.url.endswith("/api/session/config")
        and response.request.method == "POST"
        and response.request.post_data_json.get("solver_fine_pitch_mm") == 0.4
    ):
        solve_pitch.fill("0.40")
        solve_pitch.dispatch_event("change")
    persisted_pitch = page.evaluate(
        "async () => (await (await fetch('/api/session')).json()).config.solver_fine_pitch_mm"
    )
    assert persisted_pitch == 0.4


def test_settings_layout_preserves_live_control_state_and_management_surfaces(page: Page):
    page.locator("#settingsDrawerBtn").click()
    drawer = page.locator("#settingsDrawer")
    drawer.wait_for(state="visible")
    solve_pitch = page.locator("#cfgSolvePitch")
    solve_pitch.fill("0.35")
    solve_pitch.evaluate("element => { element.focus(); element.setSelectionRange(1, 4); }")

    page.set_viewport_size({"width": 1100, "height": 800})
    page.wait_for_timeout(300)
    assert solve_pitch.input_value() == "0.35"
    assert page.evaluate("document.activeElement?.id") == "cfgSolvePitch"
    assert solve_pitch.evaluate("element => [element.selectionStart, element.selectionEnd]") == [1, 4]

    advanced = page.locator("#settingsAdvancedToggle")
    advanced.click()
    assert advanced.get_attribute("aria-pressed") == "true"

    page.locator("#settingsProfileBrowseBtn").click()
    page.locator("#settingsProfileModal").wait_for(state="visible")
    page.locator("#settingsProfileModalClose").click()
    page.locator("#settingsProfileModal").wait_for(state="hidden")

    page.locator("#closeSettingsDrawer").click()
    assert drawer.get_attribute("aria-hidden") == "true"
    page.locator("#printerConfigBtn").click()
    page.locator("#printerConfigPage").wait_for(state="visible")
    page.locator("#printerConfigClose").click()
    page.locator("#printerConfigPage").wait_for(state="hidden")

    page.locator("#modelLibrariesBtn").click()
    page.locator("#modelLibrariesModal").wait_for(state="visible")
    page.locator("#modelLibrariesCloseBtn").click()
    page.locator("#modelLibrariesModal").wait_for(state="hidden")


def test_palette_modes_keep_their_own_controls(page: Page):
    page.locator('#tabSwitch .mode-button[data-tab="creation"]').click()
    auto = page.locator("#paletteModeAuto")
    manual = page.locator("#paletteModeManual")
    auto.click()
    assert auto.get_attribute("aria-selected") == "true"
    assert page.locator("#panelAutoSuggest").is_visible()

    manual.click()
    assert manual.get_attribute("aria-selected") == "true"
    assert page.locator("#panelManualBuilder").is_visible()
    assert not page.locator("#panelAutoSuggest").is_visible()


def test_image_palette_solve_and_diagnostic_lightbox_workflow(
    page: Page,
    tmp_path: Path,
):
    image_path = tmp_path / "browser-source.png"
    Image.new("RGB", (8, 6), (70, 120, 190)).save(image_path)
    page.route("**/api/run-cache/files/**", _fulfill_png)

    page.locator("#imageUploadInput").set_input_files(image_path)
    page.locator('.image-card[data-filename="browser-source.png"]').wait_for(state="visible")
    assert page.locator("#imagePreviewPane").is_visible()

    preview_image = page.locator("#previewImg")
    preview_image.wait_for(state="visible")
    light_pixels = preview_image.screenshot()
    page.locator("#themeMenuBtn").click()
    page.locator('#themeMenu [data-theme-preference="dark"]').click()
    assert preview_image.screenshot() == light_pixels

    page.locator('#tabSwitch .mode-button[data-tab="creation"]').click()
    page.locator("#paletteModeManual").click()
    first_filament = page.locator("#manualLibraryGrid .filament-card:not(.is-placed)").first
    first_filament.wait_for(state="visible")
    first_filament.click()
    assert page.locator("#mintPaletteBtn").is_enabled()
    page.locator("#mintPaletteBtn").click()
    page.locator("#railDeckList .rail-deck-card.is-active").wait_for(state="visible")
    assert page.locator("#startSolveBtn").is_enabled()

    state: dict[str, str] = {}

    def start_solve(route: Route) -> None:
        body = route.request.post_data_json
        state["card_id"] = str(body["card_id"])
        route.fulfill(json={"job_id": "browser-solve-job", "status": "running"})

    def solve_status(route: Route) -> None:
        route.fulfill(
            json={
                "status": "complete",
                "job_id": "browser-solve-job",
                "card_id": state["card_id"],
                "elapsed_s": 0.25,
                "result": _solve_result(state["card_id"]),
            }
        )

    page.route("**/api/export/files/status", lambda route: route.fulfill(json={"status": "idle"}))
    page.route("**/api/palette/validate", lambda route: route.fulfill(json={"valid": True}))
    page.route("**/api/solve/start", start_solve)
    page.route("**/api/solve/status", solve_status)
    page.locator("#startSolveBtn").click()

    result_card = page.locator('.solve-grid-column[data-solve-card-kind="run"]')
    result_card.wait_for(state="visible")
    assert page.locator("#tabSolve").is_visible()
    result_card.click()
    page.locator("#compLightbox").wait_for(state="visible")
    header = page.locator("#compLightbox .comp-lightbox-topbar")
    slider = page.locator("#compLightbox .comp-lightbox-zoom-slider")
    assert header.is_visible()
    assert slider.get_attribute("aria-label") == "Zoom"
    header_bounds = header.bounding_box()
    slider_bounds = slider.bounding_box()
    assert header_bounds is not None
    assert slider_bounds is not None

    slider.press("Home")
    minimum_header_bounds = header.bounding_box()
    minimum_slider_bounds = slider.bounding_box()
    assert minimum_header_bounds is not None
    assert minimum_slider_bounds is not None
    for key in ("x", "y", "width"):
        assert abs(minimum_header_bounds[key] - header_bounds[key]) <= 1
        assert abs(minimum_slider_bounds[key] - slider_bounds[key]) <= 1


def test_saved_run_load_and_export_initiation_workflow(page: Page):
    page.route("**/api/run-cache/files/**", _fulfill_png)
    page.route("**/api/runs/saved/*/preview", _fulfill_png)
    page.route(
        "**/api/runs/saved",
        lambda route: route.fulfill(
            json=[
                {
                    "save_id": "browser-save",
                    "tier": "saved",
                    "label": "Browser saved run",
                    "source_image_name": "browser-source.png",
                    "saved_at": "2026-07-16T12:00:00Z",
                }
            ]
        ),
    )
    page.route(
        "**/api/runs/load",
        lambda route: route.fulfill(
            json={
                "card_id": "browser-loaded-card",
                "label": "Browser saved run",
                "palette": ["bambu-basic-cyan"],
                "config": {
                    "palette": ["bambu-basic-cyan"],
                    "white_base": "panchroma-matte-cotton-white",
                    "white_cap": "panchroma-matte-cotton-white",
                    "base_filament": "panchroma-matte-cotton-white",
                    "cap_filament": "panchroma-matte-cotton-white",
                    "layer_height": 0.08,
                    "d_wb": 0.2,
                    "t_max": 2.0,
                    "image_sample_pitch_mm": 0.4,
                    "solver_fine_pitch_mm": 0.4,
                },
                "result": _solve_result("browser-loaded-card"),
            }
        ),
    )

    page.locator('#tabSwitch .mode-button[data-tab="solve"]').click()
    page.locator("#savedRunsBtn").click()
    page.locator(".saved-run-row").wait_for(state="visible")
    page.locator("#savedRunLoadBtn").click()
    page.locator('.solve-run-card[data-run-id="browser-loaded-card"]').wait_for(state="visible")
    assert page.locator("#savedRunsModal").get_attribute("aria-hidden") == "true"

    export_request: dict[str, object] = {}

    def start_export(route: Route) -> None:
        export_request.update(route.request.post_data_json)
        route.fulfill(json={"job_id": "browser-export-job", "status": "running"})

    page.route("**/api/export/files/start", start_export)
    page.route(
        "**/api/export/files/status",
        lambda route: route.fulfill(
            json={
                "status": "complete",
                "job_id": "browser-export-job",
                "elapsed_s": 0.5,
                "result": {
                    "export_id": "browser-export",
                    "output_format": "3mf",
                    "geometry_source": "field_derived",
                    "field_scale": 4,
                    "out_dir": "C:/temporary/browser-export",
                    "files": [
                        {
                            "name": "browser.3mf",
                            "url": "/api/export/files/browser.3mf?dir=browser-export",
                            "size_kb": 1,
                        }
                    ],
                    "zip_url": "/api/export/files/browser.zip?dir=browser-export",
                    "manifest": {"quality": {"color": {"is_watertight": True}}},
                    "swap_plan": {"instructions": "No swaps required"},
                },
            }
        ),
    )

    page.locator('#tabSwitch .mode-button[data-tab="export"]').click()
    assert page.locator("#exportFilesBtn").is_enabled()
    page.locator("#exportFilesBtn").click()
    page.locator('.export-record-card[data-export-record-id="browser-export"]').wait_for(
        state="visible"
    )
    assert export_request["card_id"] == "browser-loaded-card"
    assert page.locator("#exportFileList").get_by_text("browser.3mf").is_visible()


def test_solve_view_controls_and_recipe_lightbox_are_operable(page: Page):
    _load_saved_run(page, recipe=True)
    view_bar = page.locator("#solveViewBar")
    view_bar.wait_for(state="visible")

    page.locator('#solveViewToggle [data-view="white_cap"]').click()
    page.locator("#solveWhiteCapControls").wait_for(state="visible")
    page.locator('[data-solve-white-cap-view="detail_cap_map"]').click()
    assert page.locator('[data-solve-white-cap-view="detail_cap_map"]').get_attribute(
        "aria-checked"
    ) == "true"

    page.locator('#solveViewToggle [data-view="color_ceiling"]').click()
    page.locator("#solveColorRegionsControls").wait_for(state="visible")
    page.locator('[data-solve-color-regions-view="recipe_regions"]').click()
    recipe_card = page.locator('[data-solve-card-kind="recipe"]')
    recipe_card.wait_for(state="visible")
    recipe_card.click()
    page.locator("#compLightbox").wait_for(state="visible")
    assert page.locator("#compLightbox .recipe-lightbox-panel").is_visible()
    assert page.locator("#recipeRegionReadoutPanel").is_visible()
    assert page.locator("#recipeLightboxContoursToggle").get_attribute("aria-pressed") in {
        "true",
        "false",
    }
    page.locator("#compLightboxClose").click()

    page.locator("#solveAdvancedToggle").click()
    assert page.locator("#solveAdvancedToggle").get_attribute("aria-expanded") == "true"
    page.locator('#solveAdvancedViews [data-view="thickness_maps"]').click()
    page.locator('[data-solve-card-kind="thickness"]').first.wait_for(state="visible")
    page.locator('#solveAdvancedViews [data-view="surface_highpass"]').click()
    page.locator("#solveHighpassControls").wait_for(state="visible")
    page.locator('#solveViewToggle [data-view="surface_explorer"]').click()
    page.locator("#solveExplorerControls").wait_for(state="visible")


def test_management_surfaces_have_accessible_headers_and_clean_close_paths(page: Page):
    page.locator("#railLibraryBtn").click()
    active_filaments = page.locator("#libraryModalBackdrop")
    active_filaments.wait_for(state="visible")
    assert page.locator("#libraryModalClose").get_attribute("aria-label") == "Close active filaments"
    page.locator("#libraryModalClose").click()
    active_filaments.wait_for(state="hidden")

    page.locator("#settingsDrawerBtn").click()
    page.locator("#settingsProfileSaveAsBtn").click()
    save_modal = page.locator("#settingsProfileSaveModal")
    save_modal.wait_for(state="visible")
    assert page.locator("#settingsProfileSaveModalTitle").text_content() == "Save Settings Profile As"
    page.locator("#settingsProfileSaveModalCancel").click()
    save_modal.wait_for(state="hidden")
    page.locator("#closeSettingsDrawer").click()

    page.locator("#modelLibrariesBtn").click()
    model_modal = page.locator("#modelLibrariesModal")
    model_modal.wait_for(state="visible")
    assert page.locator("#modelLibrariesModal [role=\"dialog\"]").get_attribute(
        "aria-labelledby"
    ) == "modelLibrariesModalTitle"
    page.locator("#modelLibrariesCloseBtn").click()
    model_modal.wait_for(state="hidden")

    page.locator("#printerConfigBtn").click()
    printer_page = page.locator("#printerConfigPage")
    printer_page.wait_for(state="visible")
    assert page.locator("#printerConfigClose").get_attribute("aria-label") == (
        "Close printer configuration"
    )
    page.locator("#printerConfigClose").click()
    printer_page.wait_for(state="hidden")


def test_theme_system_override_reload_and_cross_tab_sync(browser: Browser, generator_url: str):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
    first = context.new_page()
    second = context.new_page()
    errors: list[str] = []
    first.on("pageerror", lambda error: errors.append(str(error)))
    second.on("pageerror", lambda error: errors.append(str(error)))
    try:
        first.goto(generator_url, wait_until="domcontentloaded")
        first.locator("#dataSourceBadge.connected").wait_for(state="visible")
        _close_recovery_modal(first)
        assert first.locator("html").get_attribute("data-theme-preference") == "system"
        assert first.locator("html").get_attribute("data-theme") == "dark"
        assert first.locator("#themeCurrentValue").text_content() == "System"

        first.locator("#themeMenuBtn").click()
        first.locator('#themeMenu [data-theme-preference="light"]').click()
        assert first.locator("html").get_attribute("data-theme") == "light"
        assert first.evaluate("localStorage.getItem('prisma_generator_theme')") == "light"
        first.reload(wait_until="domcontentloaded")
        first.locator("#dataSourceBadge.connected").wait_for(state="visible")
        _close_recovery_modal(first)
        assert first.locator("html").get_attribute("data-theme") == "light"

        second.goto(generator_url, wait_until="domcontentloaded")
        second.locator("#dataSourceBadge.connected").wait_for(state="visible")
        _close_recovery_modal(second)
        assert second.locator("html").get_attribute("data-theme") == "light"

        first.locator("#themeMenuBtn").click()
        first.locator('#themeMenu [data-theme-preference="dark"]').click()
        second.locator('html[data-theme="dark"]').wait_for(state="attached")
        assert second.locator("#themeCurrentValue").text_content() == "Dark"

        first.locator("#themeMenuBtn").click()
        first.locator('#themeMenu [data-theme-preference="system"]').click()
        first.emulate_media(color_scheme="light")
        first.locator('html[data-theme="light"]').wait_for(state="attached")
        first.emulate_media(color_scheme="dark")
        first.locator('html[data-theme="dark"]').wait_for(state="attached")
        assert errors == []
    finally:
        context.close()


def test_theme_control_is_grouped_with_right_side_utilities(page: Page):
    settings_bounds = page.locator("#settingsDrawerBtn").bounding_box()
    theme_bounds = page.locator("#themeMenuBtn").bounding_box()
    clear_bounds = page.locator("#clearAllTempBtn").bounding_box()
    assert settings_bounds is not None
    assert theme_bounds is not None
    assert clear_bounds is not None
    assert theme_bounds["x"] > settings_bounds["x"] + settings_bounds["width"] + 12
    assert 0 <= clear_bounds["x"] - (theme_bounds["x"] + theme_bounds["width"]) <= 8


def test_theme_popover_keyboard_and_narrow_viewport_geometry(browser: Browser, generator_url: str):
    context = browser.new_context(viewport={"width": 820, "height": 900}, color_scheme="light")
    instance = context.new_page()
    page_errors: list[str] = []
    instance.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        instance.goto(generator_url, wait_until="domcontentloaded")
        instance.locator("#dataSourceBadge.connected").wait_for(state="visible")
        _close_recovery_modal(instance)
        button = instance.locator("#themeMenuBtn")
        button.focus()
        button.press("ArrowDown")
        menu = instance.locator("#themeMenu")
        assert menu.is_visible()
        assert instance.evaluate("document.activeElement?.dataset.themePreference") == "system"
        instance.keyboard.press("End")
        assert instance.evaluate("document.activeElement?.dataset.themePreference") == "dark"
        instance.keyboard.press("Enter")
        assert instance.locator("html").get_attribute("data-theme") == "dark"
        assert not menu.is_visible()
        assert instance.evaluate("document.activeElement?.id") == "themeMenuBtn"

        button.press("ArrowUp")
        assert instance.evaluate("document.activeElement?.dataset.themePreference") == "dark"
        instance.keyboard.press("Escape")
        assert not menu.is_visible()
        assert instance.evaluate("document.activeElement?.id") == "themeMenuBtn"

        button.click()
        bounds = menu.bounding_box()
        assert bounds is not None
        assert bounds["x"] >= 0
        assert bounds["y"] >= 0
        assert bounds["x"] + bounds["width"] <= 820
        assert bounds["y"] + bounds["height"] <= 900
        assert instance.locator(".theme-button-prefix").evaluate("el => getComputedStyle(el).display") == "none"
        assert instance.locator("#themeCurrentValue").is_visible()
        assert page_errors == []
    finally:
        context.close()
