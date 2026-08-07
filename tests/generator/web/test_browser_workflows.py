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
from playwright.sync_api import Browser, Page, Route, TimeoutError as PlaywrightTimeoutError, sync_playwright
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


def _suppress_first_launch_offer(context) -> None:
    """Keep ordinary browser workflows independent of onboarding state."""
    context.route(
        "**/api/guides/state",
        lambda route: route.fulfill(
            json={
                "schema_version": 2,
                "revision": 0,
                "welcome_status": "declined",
            }
        ) if route.request.method == "GET" else route.continue_(),
    )


@pytest.fixture
def page(browser: Browser, generator_url: str) -> Page:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    _suppress_first_launch_offer(context)
    instance = context.new_page()
    instance.set_default_timeout(10_000)
    page_errors: list[str] = []
    instance.on("pageerror", lambda error: page_errors.append(getattr(error, "stack", str(error))))
    instance.goto(generator_url, wait_until="domcontentloaded")
    try:
        instance.locator("#dataSourceBadge.connected").wait_for(state="visible")
    except PlaywrightTimeoutError as exc:
        badge = instance.locator("#dataSourceBadge").text_content()
        raise AssertionError(
            f"Generator did not connect; badge={badge!r}; page_errors={page_errors!r}"
        ) from exc
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


def _confirm_guide_start_if_needed(page: Page) -> None:
    dialog = page.locator("#appDialog")
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if dialog.get_attribute("aria-hidden") == "false":
            assert "Clear Workspace for Guide" in page.locator("#appDialogTitle").text_content()
            page.locator("#appDialogYes").click()
            return
        if page.locator("#guideOverlayRoot").get_attribute("aria-hidden") == "false":
            return
        page.wait_for_timeout(25)


def _start_catalog_guide(page: Page, title: str) -> None:
    page.locator("#helpGuidesMenuBtn").click()
    page.locator('[data-help-guides-action="catalog"]').click()
    catalog = page.locator("#guidesCatalogModal")
    catalog.wait_for(state="visible")
    entry = page.locator(".summary-action-row").filter(has_text=title)
    assert entry.count() == 1
    entry.get_by_role("button", name=f"Start {title}").click()
    _confirm_guide_start_if_needed(page)
    page.locator("#guideOverlayRoot").wait_for(state="visible")


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


@pytest.mark.parametrize(
    "title",
    [
        "First-Time Setup",
        "Prisma Generator Basics",
        "Image Guide",
        "Palette Guide",
        "Settings Guide",
        "Preview Guide",
        "Export Guide",
        "Saving & Loading",
        "Explore the Interface",
    ],
)
def test_every_catalog_guide_launches_and_can_exit_early(page: Page, title: str):
    _start_catalog_guide(page, title)
    assert page.locator("#guideStepTitle").text_content().strip()
    page.locator("#guideStepEnd").click()
    page.locator("#guideOverlayRoot").wait_for(state="hidden")


def test_saving_loading_prepared_workspace_advances(page: Page):
    _start_catalog_guide(page, "Saving & Loading")
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "Review the prepared workspace"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "Saved Palettes and the Palette Deck"
    page.locator("#guideStepEnd").click()
    page.locator("#guideOverlayRoot").wait_for(state="hidden")


def test_basics_introduces_lithophane_principles_before_the_ui_workflow(page: Page):
    _start_catalog_guide(page, "Prisma Generator Basics")
    assert page.locator("#guideStepTitle").text_content() == "Prisma Generator Basics"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "Terminology"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "How a lithophane creates an image"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "How Prisma turns an image into a print"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "The Generator workflow"
    page.wait_for_function(
        "document.querySelector('#guideStepCard')?.dataset.placement === 'viewport-center'"
    )
    centered = page.evaluate(
        """() => {
            const card = document.querySelector('#guideStepCard').getBoundingClientRect();
            return {
                x: (card.left + card.width / 2) - (window.innerWidth / 2),
                y: (card.top + card.height / 2) - (window.innerHeight / 2),
            };
        }"""
    )
    assert abs(centered["x"]) <= 1
    assert abs(centered["y"]) <= 1
    page.locator("#guideStepEnd").click()
    page.locator("#guideOverlayRoot").wait_for(state="hidden")


def test_reloading_an_active_guide_recovers_without_false_other_window(page: Page):
    _start_catalog_guide(page, "Saving & Loading")
    page_id_before_reload = page.evaluate(
        "sessionStorage.getItem('prisma.generator.page-id')"
    )
    assert page_id_before_reload
    page.reload(wait_until="domcontentloaded")

    page.locator("#dataSourceBadge.connected").wait_for(state="visible")
    page.locator("#workspaceLockInterstitial").wait_for(state="hidden")
    page.locator("#guideOverlayRoot").wait_for(state="hidden")
    assert page.evaluate(
        "sessionStorage.getItem('prisma.generator.page-id')"
    ) == page_id_before_reload
    assert "Another Prisma window" not in page.locator("body").inner_text()


def test_workspace_lock_interstitial_uses_an_opaque_modal_surface(page: Page):
    interstitial = page.locator("#workspaceLockInterstitial")
    interstitial.evaluate("element => { element.classList.remove('is-hidden'); element.setAttribute('aria-hidden', 'false'); }")
    window = page.locator(".workspace-lock-window")

    assert "modal-dialog" in (window.get_attribute("class") or "").split()
    assert window.evaluate("element => getComputedStyle(element).backgroundColor") != "rgba(0, 0, 0, 0)"
    assert window.evaluate("element => getComputedStyle(element).borderRadius") != "0px"


def test_guided_setup_completes_through_real_browser_controls(page: Page):
    _start_catalog_guide(page, "First-Time Setup")
    assert page.locator("#guideStepTitle").text_content() == "Configure your printer"
    page.wait_for_function(
        "document.querySelector('#guideStepCard')?.dataset.placement === 'viewport-center'"
    )
    opening_layout = page.evaluate(
        """() => {
            const card = document.querySelector('#guideStepCard').getBoundingClientRect();
            const button = document.querySelector('#printerConfigBtn').getBoundingClientRect();
            const frame = document.querySelector('.guide-target-frame:not(.is-hidden)').getBoundingClientRect();
            return {
                cardCenterX: card.left + card.width / 2,
                cardCenterY: card.top + card.height / 2,
                viewportCenterX: window.innerWidth / 2,
                viewportCenterY: window.innerHeight / 2,
                button: { left: button.left, top: button.top, right: button.right, bottom: button.bottom },
                frame: { left: frame.left, top: frame.top, right: frame.right, bottom: frame.bottom },
            };
        }"""
    )
    assert abs(opening_layout["cardCenterX"] - opening_layout["viewportCenterX"]) <= 1
    assert abs(opening_layout["cardCenterY"] - opening_layout["viewportCenterY"]) <= 1
    for edge in ("left", "top", "right", "bottom"):
        assert abs(abs(opening_layout["frame"][edge] - opening_layout["button"][edge]) - 4) <= 1

    page.locator("#printerConfigBtn").click()
    page.locator("#printerConfigPage").wait_for(state="visible")
    assert page.locator("#guideStepTitle").text_content() == "Review printer setup"
    page.wait_for_function(
        """() => {
            const fields = document.querySelector('[data-guide-target="printer.configuration-fields"]')?.getBoundingClientRect();
            const frame = document.querySelector('.guide-target-frame:not(.is-hidden)')?.getBoundingClientRect();
            return fields && frame && Math.abs(frame.left - (fields.left - 4)) <= 1;
        }"""
    )
    configuration_layout = page.evaluate(
        """() => {
            const fields = document.querySelector('[data-guide-target="printer.configuration-fields"]').getBoundingClientRect();
            const windowRect = document.querySelector('[data-guide-target="printer.configuration"]').getBoundingClientRect();
            const frames = [...document.querySelectorAll('.guide-target-frame:not(.is-hidden)')];
            const frame = frames[0].getBoundingClientRect();
            return {
                frameCount: frames.length,
                fields: { left: fields.left, top: fields.top, right: fields.right, bottom: fields.bottom },
                frame: { left: frame.left, top: frame.top, right: frame.right, bottom: frame.bottom },
                frameWidth: frame.width,
                frameHeight: frame.height,
                windowWidth: windowRect.width,
                windowHeight: windowRect.height,
            };
        }"""
    )
    assert configuration_layout["frameCount"] == 1
    for edge in ("left", "top", "right", "bottom"):
        assert abs(abs(configuration_layout["frame"][edge] - configuration_layout["fields"][edge]) - 4) <= 1
    assert configuration_layout["frameWidth"] < configuration_layout["windowWidth"]
    assert configuration_layout["frameHeight"] < configuration_layout["windowHeight"]
    page.locator("#guideStepPrevious").click()
    assert page.locator("#guideStepTitle").text_content() == "Configure your printer"
    page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "Review printer setup"
    page.locator("#printerConfigClose").click()
    page.locator("#printerConfigPage").wait_for(state="hidden")
    assert page.locator("#guideStepTitle").text_content() == "Changing the Nozzle Size"

    for _ in range(6):
        page.locator("#guideStepNext").click()
    assert page.locator("#guideStepTitle").text_content() == "Ready to create"
    followup_centering = page.evaluate(
        """() => {
            const panel = document.querySelector('#guideStepFollowup').getBoundingClientRect();
            const button = document.querySelector('#guideStepFollowupButton').getBoundingClientRect();
            return (button.left + button.width / 2) - (panel.left + panel.width / 2);
        }"""
    )
    assert abs(followup_centering) <= 1
    page.locator("#guideStepNext").click()
    page.locator("#guideOverlayRoot").wait_for(state="hidden")


def test_palette_deck_create_variant_workflow(page: Page):
    page.locator('#tabSwitch .mode-button[data-tab="creation"]').click()
    page.locator("#paletteModeManual").click()

    first = page.locator("#manualLibraryGrid .filament-card:not(.is-placed)").first
    first.wait_for(state="visible")
    first.click()
    page.locator("#manualLibraryGrid .filament-card:not(.is-placed)").first.click()
    page.locator("#mintPaletteBtn").click()

    source = page.locator("#railDeckList .rail-deck-card").first
    source.wait_for(state="visible")
    source_title = source.locator(".rail-deck-card-title").text_content()
    assert source.locator(".rail-deck-remove").is_visible()

    menu_button = source.locator(".rail-deck-menu-button")
    menu_button.click()
    menu = page.locator("#deckCardMenu")
    menu.wait_for(state="visible")
    assert menu.locator('[data-deck-card-action="variant"]').is_visible()
    assert menu.locator('[data-deck-card-action="save"]').is_visible()
    menu.locator('[data-deck-card-action="variant"]').press("Escape")
    menu.wait_for(state="hidden")
    assert menu_button.evaluate("button => document.activeElement === button")

    menu_button.click()
    menu.locator('[data-deck-card-action="save"]').click()
    save_modal = page.locator("#paletteSaveModal")
    save_modal.wait_for(state="visible")
    page.locator("#paletteSaveCancel").click()
    save_modal.wait_for(state="hidden")

    menu_button.click()
    menu.locator('[data-deck-card-action="variant"]').click()
    assert page.locator("#manualPaletteTitle").text_content() == f"Variant of “{source_title}”"
    assert page.locator("#mintPaletteBtn").text_content() == "Add Variant to Deck"
    assert page.locator("#clearComposerBtn").text_content() == "Cancel Variant"
    assert page.locator("#mintPaletteBtn").is_disabled()

    page.locator("#manualAmsSlots .ams-slot-remove").first.click()
    # The first available card is the just-removed source filament; choose the
    # next one so the ordered palette actually differs.
    page.locator("#manualLibraryGrid .filament-card:not(.is-placed)").nth(1).click()
    assert page.locator("#mintPaletteBtn").is_enabled()
    page.locator("#mintPaletteBtn").click()

    deck_cards = page.locator("#railDeckList .rail-deck-card")
    assert deck_cards.count() == 2
    assert deck_cards.nth(0).locator(".rail-deck-card-title").text_content() == source_title
    assert deck_cards.nth(1).locator(".rail-deck-card-title").text_content() == f"{source_title} Variant"
    assert deck_cards.nth(1).get_attribute("aria-selected") == "true"
    assert page.locator("#manualPaletteTitle").text_content() == "Manual Palette"


def test_run_history_cards_save_naming_and_stable_controls(page: Page):
    _load_saved_run(page)
    page.route(
        "**/api/runs/save",
        lambda route: route.fulfill(
            json={"save_id": "browser-save-authoritative", "label": "Server Portrait"}
        ),
    )

    cards = page.locator("#solveRunCards")
    card = page.locator('.solve-run-card[data-run-id="browser-loaded-card"]')
    assert cards.get_attribute("role") == "listbox"
    assert cards.get_attribute("aria-multiselectable") == "true"
    assert card.get_attribute("role") == "option"
    assert card.get_attribute("aria-selected") == "true"
    assert card.locator(".solve-run-loaded-badge").count() == 0

    card.press(" ")
    assert card.get_attribute("aria-selected") == "false"
    card.press("Enter")
    assert card.get_attribute("aria-selected") == "true"

    card.locator(".solve-run-save-btn").click()
    prompt = page.locator("#appDialogInput")
    assert prompt.input_value() == "Browser saved run"
    prompt.fill("Run 007")
    page.locator("#appDialogYes").click()
    assert page.locator("#appDialog").get_attribute("aria-hidden") == "false"
    assert "reserved for automatic run labels" in page.locator(
        ".app-dialog-validation"
    ).text_content()

    prompt.fill("Portrait")
    page.locator("#appDialogYes").click()
    page.locator(".solve-run-label", has_text="Server Portrait").wait_for(
        state="visible"
    )
    assert card.locator(".solve-run-label").text_content() == "Server Portrait"

    page.locator("#savedRunsBtn").click()
    page.locator(".saved-run-row").wait_for(state="visible")
    page.locator("#savedRunRenameBtn").click()
    rename_modal = page.locator("#renameSavedRunModal")
    rename_modal.wait_for(state="visible")
    page.locator("#renameSavedRunDisplay").fill(" run 7 ")
    page.locator("#renameSavedRunSubmit").click()
    assert rename_modal.is_visible()
    assert "reserved for automatic run labels" in page.locator(
        "#renameSavedRunValidation"
    ).text_content()
    page.locator("#renameSavedRunCancelBtn").click()
    rename_modal.wait_for(state="hidden")
    page.locator("#savedRunsCloseBtn").click()

    card_header = card.locator(".solve-run-card-header")
    delete_button = card.locator(".solve-run-delete-btn")
    header_before_delete = card_header.bounding_box()
    delete_before = delete_button.bounding_box()
    assert delete_before["width"] == 18
    delete_button.click()
    assert delete_button.text_content() == "!"
    header_after_delete = card_header.bounding_box()
    delete_after = delete_button.bounding_box()
    assert header_before_delete == header_after_delete
    assert delete_before == delete_after

    clear_button = page.locator("#clearSolveHistoryBtn")
    solve_header = page.locator("#tabSolve .solve-deck-sidebar .deck-header")
    header_before_clear = solve_header.bounding_box()
    clear_before = clear_button.bounding_box()
    assert clear_before["width"] == 42
    clear_button.click()
    assert clear_button.text_content() == "Clear?"
    assert page.locator("#exportClearSolveHistoryBtn").text_content() == "Clear?"
    assert clear_button.evaluate("element => element.scrollWidth <= element.clientWidth")
    assert solve_header.bounding_box() == header_before_clear
    assert clear_button.bounding_box() == clear_before
    clear_button.click()
    assert clear_button.is_disabled()
    assert page.locator("#exportClearSolveHistoryBtn").is_disabled()


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


def test_batch_phone_image_import_reports_partial_success(
    page: Page,
    tmp_path: Path,
):
    fixture = (
        _ROOT / "tests" / "fixtures" / "heif" / "RGB_8__29x100.heif.b64"
    )
    phone_image = tmp_path / "browser-phone.heic"
    phone_image.write_bytes(base64.b64decode(fixture.read_text(encoding="ascii")))
    jpeg_alias = tmp_path / "browser-alias.jfif"
    Image.new("RGB", (7, 5), (120, 80, 40)).save(jpeg_alias, format="JPEG")
    corrupt = tmp_path / "browser-corrupt.heic"
    corrupt.write_bytes(b"not an image")

    page.locator("#imageUploadInput").set_input_files(
        [phone_image, jpeg_alias, corrupt]
    )
    page.locator('.image-card[data-filename="browser-phone.heic"]').wait_for(
        state="visible"
    )
    page.locator('.image-card[data-filename="browser-alias.jfif"]').wait_for(
        state="visible"
    )
    notice = page.locator("#imageImportNotice")
    notice.get_by_text("1 image could not be prepared").wait_for(state="visible")
    assert page.locator('.image-card[data-filename="browser-corrupt.heic"]').count() == 0
    assert page.locator("#previewImg").is_visible()

    page.locator("#imageImportDetailsBtn").click()
    modal = page.locator("#imageImportIssuesModal")
    modal.wait_for(state="visible")
    assert modal.get_by_text("browser-corrupt.heic", exact=True).is_visible()
    assert modal.get_by_text("Cannot decode", exact=False).is_visible()
    page.locator("#imageImportIssuesDone").click()
    modal.wait_for(state="hidden")


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
    active_palette_card = page.locator("#railDeckList .rail-deck-card.is-active")
    active_palette_card.wait_for(state="visible")
    assert active_palette_card.locator(".rail-deck-remove").bounding_box()["width"] == 18
    palette_clear = page.locator("#railClearDeckBtn")
    palette_clear_before = palette_clear.bounding_box()
    assert palette_clear_before["width"] == 42
    palette_clear.click()
    assert palette_clear.text_content() == "Clear?"
    assert palette_clear.bounding_box() == palette_clear_before
    assert palette_clear.evaluate("element => element.scrollWidth <= element.clientWidth")
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
    remove_button_styles = page.evaluate(
        """() => {
            const properties = [
                "width", "height", "padding", "border", "borderRadius",
                "backgroundColor", "color",
            ];
            const stylesFor = (selector) => {
                const style = getComputedStyle(document.querySelector(selector));
                return Object.fromEntries(properties.map((property) => [property, style[property]]));
            };
            return {
                palette: stylesFor(".rail-deck-remove"),
                solve: stylesFor(".solve-run-delete-btn"),
            };
        }"""
    )
    assert remove_button_styles["solve"] == remove_button_styles["palette"]
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


def test_solve_grid_warning_cancel_and_acceptance_workflow(
    page: Page,
    tmp_path: Path,
):
    image_path = tmp_path / "browser-grid-warning.png"
    Image.new("RGB", (8, 8), (80, 130, 180)).save(image_path)
    page.locator("#imageUploadInput").set_input_files(image_path)
    page.locator('.image-card[data-filename="browser-grid-warning.png"]').wait_for(
        state="visible"
    )

    page.locator('#arButtonGroup [data-ar="specified"]').click()
    for selector in ("#outputWidthMm", "#outputHeightMm"):
        field = page.locator(selector)
        field.fill("100.2")
        field.dispatch_event("change")

    page.locator("#settingsDrawerBtn").click()
    solve_pitch = page.locator("#cfgSolvePitch")
    with page.expect_response(
        lambda response: response.url.endswith("/api/session/config")
        and response.request.method == "POST"
        and response.request.post_data_json.get("solver_fine_pitch_mm") == 0.4
    ):
        solve_pitch.fill("0.4")
        solve_pitch.dispatch_event("change")
    page.locator("#closeSettingsDrawer").click()

    warning = page.locator("#imageSolveGridWarning")
    warning.wait_for(state="visible")
    assert warning.text_content() == (
        "⚠ Width & Height must be divisible by the 0.4 mm Solve Pitch. "
        "If not changed, the current Width & Height will be rounded to 100.4 × 100.4 mm."
    )
    assert page.locator("#outputWidthSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "visible"
    assert page.locator("#outputHeightSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "visible"
    assert page.locator('#infoPrintSize [data-axis="width"]').evaluate(
        "element => element.classList.contains('is-solve-grid-incompatible')"
    )
    assert page.locator('#infoPrintSize [data-axis="height"]').evaluate(
        "element => element.classList.contains('is-solve-grid-incompatible')"
    )
    assert page.locator('#infoSolvePx [data-axis="width"]').evaluate(
        "element => element.classList.contains('is-solve-grid-incompatible')"
    )
    assert page.locator('#infoSolvePx [data-axis="height"]').evaluate(
        "element => element.classList.contains('is-solve-grid-incompatible')"
    )
    assert page.locator("#infoPrintSize").text_content().startswith("100.4 × 100.4 mm")
    assert page.locator("#infoSolvePx").text_content() == "251 × 251 = 63,001 px"

    page.locator('#tabSwitch .mode-button[data-tab="creation"]').click()
    page.locator("#paletteModeManual").click()
    page.locator("#manualLibraryGrid .filament-card:not(.is-placed)").first.click()
    page.locator("#mintPaletteBtn").click()

    starts: list[dict] = []

    def start_solve(route: Route) -> None:
        starts.append(route.request.post_data_json)
        route.fulfill(json={"job_id": "grid-warning-solve", "status": "running"})

    page.route("**/api/export/files/status", lambda route: route.fulfill(json={"status": "idle"}))
    page.route("**/api/palette/validate", lambda route: route.fulfill(json={"valid": True}))
    page.route("**/api/solve/start", start_solve)
    page.route(
        "**/api/solve/status",
        lambda route: route.fulfill(
            json={
                "status": "complete",
                "job_id": "grid-warning-solve",
                "card_id": starts[-1]["card_id"] if starts else "pending",
                "elapsed_s": 0.1,
                "result": _solve_result(starts[-1]["card_id"] if starts else "pending"),
            }
        ),
    )

    page.locator("#startSolveBtn").click()
    dialog = page.locator("#appDialog")
    page.wait_for_function("() => document.querySelector('#appDialog').getAttribute('aria-hidden') === 'false'")
    assert page.locator("#appDialogTitle").text_content() == "Error: Invalid Lithophane Size"
    assert page.locator("#appDialogMsg").text_content() == (
        "The requested lithophane size is incompatible with the selected 0.4 mm Solve Pitch because neither "
        "the width nor the height resolves to a whole number of solve pixels.\n\n"
        "Requested size: 100.2 × 100.2 mm\nAdjusted size: 100.4 × 100.4 mm (251 × 251 px)\n\n"
        "Select Accept & Continue to apply the adjusted size. Select Cancel to return to the Image page "
        "without changing the dimensions."
    )
    assert page.locator("#appDialogMsg .app-dialog-emphasis").all_text_contents() == [
        "0.4 mm",
        "100.2 × 100.2 mm",
        "100.4 × 100.4 mm (251 × 251 px)",
    ]
    page.locator("#appDialogNo").click()
    page.wait_for_function("() => document.querySelector('#appDialog').getAttribute('aria-hidden') === 'true'")
    assert page.locator("#outputWidthMm").input_value() == "100.2"
    assert page.locator("#outputHeightMm").input_value() == "100.2"
    assert page.locator("#outputWidthSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "visible"
    assert page.locator("#outputHeightSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "visible"
    assert starts == []

    page.locator("#startSolveBtn").click()
    page.wait_for_function("() => document.querySelector('#appDialog').getAttribute('aria-hidden') === 'false'")
    assert page.locator("#appDialogYes").text_content() == "Accept & Continue"
    page.locator("#appDialogYes").click()
    page.wait_for_function("() => document.querySelector('#outputWidthMm').value === '100.4'")
    page.wait_for_function("() => document.querySelector('#outputHeightMm').value === '100.4'")
    page.wait_for_function("() => document.querySelector('#arButtonGroup [data-ar=\"specified\"]').classList.contains('is-active')")
    page.locator("#tabSolve").wait_for(state="visible")
    assert len(starts) == 1
    assert warning.is_hidden()
    assert page.locator("#outputWidthSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "hidden"
    assert page.locator("#outputHeightSolveGridWarning").evaluate(
        "element => getComputedStyle(element).visibility"
    ) == "hidden"


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


def test_private_saved_run_source_stays_out_of_library_and_survives_reload(page: Page):
    source_image = {
        "filename": "archived-portrait.png",
        "width": 4,
        "height": 4,
        "size_kb": 0.1,
        "thumbnail_url": (
            "/api/images/preview/archived-portrait.png"
            "?image_source_ref=loaded-run%3Abrowser-private-card"
        ),
        "source_ref": "loaded-run:browser-private-card",
        "temporary": True,
    }
    loaded_config = {
        "image_path": "archived-portrait.png",
        "image_source_ref": "loaded-run:browser-private-card",
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
    }
    config_writes: list[dict] = []

    page.route("**/api/run-cache/files/**", _fulfill_png)
    page.route("**/api/images/preview/**", _fulfill_png)
    page.route(
        "**/api/images",
        lambda route: route.fulfill(
            json=[{
                "filename": "library-only.png",
                "width": 4,
                "height": 4,
                "size_kb": 0.1,
            }]
        ),
    )
    page.route(
        "**/api/runs/saved",
        lambda route: route.fulfill(
            json=[{
                "save_id": "browser-private-save",
                "tier": "saved",
                "label": "Private source run",
                "source_image_name": "archived-portrait.png",
                "saved_at": "2026-07-25T12:00:00Z",
            }]
        ),
    )
    page.route("**/api/runs/saved/*/preview", _fulfill_png)
    page.route(
        "**/api/runs/load",
        lambda route: route.fulfill(
            json={
                "card_id": "browser-private-card",
                "label": "Private source run",
                "palette": ["bambu-basic-cyan"],
                "config": loaded_config,
                "source_image": source_image,
                "result": _solve_result("browser-private-card"),
            }
        ),
    )

    def update_config(route: Route) -> None:
        payload = route.request.post_data_json
        config_writes.append(payload)
        route.fulfill(json={"config": payload})

    page.route("**/api/session/config", update_config)
    page.locator('#tabSwitch .mode-button[data-tab="solve"]').click()
    page.locator("#savedRunsBtn").click()
    page.locator(".saved-run-row").wait_for(state="visible")
    page.locator("#savedRunLoadBtn").click()
    page.locator('.solve-run-card[data-run-id="browser-private-card"]').wait_for(
        state="visible"
    )

    page.locator('#tabSwitch .mode-button[data-tab="image"]').click()
    notice = page.locator("#savedRunSourceNotice")
    notice.wait_for(state="visible")
    assert page.locator("#savedRunSourceNoticeName").text_content() == "archived-portrait.png"
    assert page.locator("#imageGrid .image-card").count() == 1
    assert page.locator('#imageGrid [data-filename^="loaded-"]').count() == 0
    assert config_writes[-1]["image_source_ref"] == "loaded-run:browser-private-card"

    page.route(
        "**/api/session",
        lambda route: route.fulfill(
            json={
                "config": loaded_config,
                "source_image": source_image,
                "solve": {"status": "idle", "elapsed_s": 0, "result": None},
            }
        ),
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#dataSourceBadge.connected").wait_for(state="visible")
    _close_recovery_modal(page)
    page.locator("#savedRunSourceNotice").wait_for(state="visible")
    assert page.locator("#imageGrid .image-card").count() == 1

    page.locator('#imageGrid [data-filename="library-only.png"]').click()
    page.locator("#savedRunSourceNotice").wait_for(state="hidden")
    page.wait_for_function(
        "() => document.querySelector('[data-filename=\"library-only.png\"]')"
        "?.classList.contains('is-selected')"
    )
    assert config_writes[-1]["image_source_ref"] is None


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


def test_printer_minimum_length_is_whole_nozzle_derived_and_blocks_invalid_exit(
    page: Page,
):
    page.locator("#printerConfigBtn").click()
    printer_page = page.locator("#printerConfigPage")
    printer_page.wait_for(state="visible")

    table = page.locator("#pcNozzleTable")
    assert "Min W" not in table.locator("thead").inner_text()
    multiplier = table.locator(".nz-min-ll-mult").first
    nozzle = table.locator(".nz-size").first
    derived = table.locator(".nz-min-ll-derived").first
    assert multiplier.get_attribute("min") == "2"
    assert multiplier.get_attribute("max") == "10"
    assert multiplier.get_attribute("step") == "1"
    assert "pcNozzleLengthHelp" in (multiplier.get_attribute("aria-describedby") or "")

    nozzle.fill("0.2")
    multiplier.fill("3")
    assert derived.text_content() == "× nozzle = 0.6 mm"

    multiplier.fill("1")
    page.locator("#printerConfigClose").click()
    assert printer_page.is_visible()
    assert table.locator("tr.is-invalid").count() == 1

    multiplier.fill("2")
    page.locator("#printerConfigClose").click()
    printer_page.wait_for(state="hidden")


def test_theme_system_override_reload_and_cross_tab_sync(browser: Browser, generator_url: str):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
    _suppress_first_launch_offer(context)
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
    _suppress_first_launch_offer(context)
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


def test_solve_mode_split_button_keyboard_and_narrow_geometry(
    browser: Browser,
    generator_url: str,
):
    context = browser.new_context(viewport={"width": 820, "height": 900}, color_scheme="light")
    _suppress_first_launch_offer(context)
    instance = context.new_page()
    page_errors: list[str] = []
    instance.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        instance.goto(generator_url, wait_until="domcontentloaded")
        instance.locator("#dataSourceBadge.connected").wait_for(state="visible")
        _close_recovery_modal(instance)

        menu_button = instance.locator("#solveModeMenuBtn")
        menu = instance.locator("#solveModeMenu")
        menu_button.focus()
        menu_button.press("ArrowDown")
        assert menu.is_visible()
        assert instance.evaluate("document.activeElement?.dataset.solveMode") == "single"
        instance.keyboard.press("End")
        assert instance.evaluate("document.activeElement?.dataset.solveMode") == "batch"
        instance.keyboard.press("Enter")
        assert not menu.is_visible()
        assert instance.locator("#startSolveBtn").text_content() == "Batch Solve (0)"
        assert instance.evaluate("document.activeElement?.id") == "solveModeMenuBtn"

        menu_button.press("ArrowUp")
        assert instance.evaluate("document.activeElement?.dataset.solveMode") == "batch"
        instance.keyboard.press("Escape")
        assert not menu.is_visible()
        assert instance.evaluate("document.activeElement?.id") == "solveModeMenuBtn"

        menu_button.click()
        menu_bounds = menu.bounding_box()
        split_bounds = instance.locator("#solveActionSplit").bounding_box()
        assert menu_bounds is not None
        assert split_bounds is not None
        assert menu_bounds["x"] >= 0
        assert menu_bounds["y"] >= 0
        assert menu_bounds["x"] + menu_bounds["width"] <= 820
        assert menu_bounds["y"] + menu_bounds["height"] <= 900
        assert split_bounds["x"] >= 0
        assert split_bounds["x"] + split_bounds["width"] <= 820

        instance.locator('#themeMenuBtn').click()
        instance.locator('#themeMenu [data-theme-preference="dark"]').click()
        assert instance.locator("html").get_attribute("data-theme") == "dark"
        assert page_errors == []
    finally:
        context.close()
