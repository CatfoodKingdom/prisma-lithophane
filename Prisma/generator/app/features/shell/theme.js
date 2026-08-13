import {
  THEME_PREFERENCES,
  THEME_STORAGE_KEY,
  applyResolvedTheme,
  normalizeThemePreference,
} from "../../core/theme.js";

/**
 * Install Generator theme ownership without publishing browser globals.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesShellTheme(app) {
  let initialized = false;
  let root = null;
  let mediaQuery = null;
  let button = null;
  let menu = null;
  let viewportTarget = null;

  function themeLabel(preference) {
    return preference.charAt(0).toUpperCase() + preference.slice(1);
  }

  function systemIsDark() {
    return Boolean(mediaQuery?.matches);
  }

  function updateThemeMenuUi() {
    const preference = app.state.ui.themePreference;
    const currentValue = app.state.ui.$?.("#themeCurrentValue");
    if (currentValue) currentValue.textContent = themeLabel(preference);
    if (button) {
      button.setAttribute("aria-label", `Theme: ${themeLabel(preference)}`);
      button.title = `Theme: ${themeLabel(preference)}`;
    }
    menu?.querySelectorAll("[data-theme-preference]").forEach((item) => {
      item.setAttribute("aria-checked", item.dataset.themePreference === preference ? "true" : "false");
    });
  }

  function applyThemePreference(preference, { persist = true } = {}) {
    const normalized = normalizeThemePreference(preference);
    if (persist) {
      try { app.persistence.write(THEME_STORAGE_KEY, normalized); } catch { /* unavailable storage is non-fatal */ }
    }
    app.state.ui.themePreference = normalized;
    app.state.ui.themeResolved = applyResolvedTheme(root, normalized, systemIsDark());
    updateThemeMenuUi();
    return app.state.ui.themeResolved;
  }

  function themeMenuItems() {
    return [...(menu?.querySelectorAll("[data-theme-preference]") || [])];
  }

  function positionThemeMenu() {
    if (!button || !menu || menu.hidden) return;
    const rect = button.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const menuWidth = menu.offsetWidth;
    const menuHeight = menu.offsetHeight;
    const viewportWidth = viewportTarget?.innerWidth ?? document.documentElement.clientWidth;
    const viewportHeight = viewportTarget?.innerHeight ?? document.documentElement.clientHeight;
    const left = Math.max(margin, Math.min(rect.right - menuWidth, viewportWidth - menuWidth - margin));
    const below = rect.bottom + gap;
    const top = below + menuHeight <= viewportHeight - margin
      ? below
      : Math.max(margin, rect.top - menuHeight - gap);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  function closeThemeMenu({ restoreFocus = false } = {}) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    button?.setAttribute("aria-expanded", "false");
    if (restoreFocus) button?.focus();
  }

  function openThemeMenu({ focus = "checked" } = {}) {
    if (!menu || !button) return;
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    positionThemeMenu();
    const items = themeMenuItems();
    if (!items.length) return;
    const target = focus === "first"
      ? items[0]
      : focus === "last"
        ? items.at(-1)
        : items.find((item) => item.getAttribute("aria-checked") === "true") || items[0];
    target.focus({ preventScroll: true });
  }

  function toggleThemeMenu() {
    if (menu?.hidden) openThemeMenu(); else closeThemeMenu({ restoreFocus: true });
  }

  function handleThemeMenuKeydown(event) {
    const items = themeMenuItems();
    if (!items.length) return;
    const currentIndex = Math.max(0, items.indexOf(event.target));
    let nextIndex = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeThemeMenu({ restoreFocus: true });
      return;
    }
    if (event.key === "Tab") closeThemeMenu();
  }

  function initializeThemeController({
    documentRoot = document.documentElement,
    systemTheme = window.matchMedia("(prefers-color-scheme: dark)"),
    storageEvents = window,
    documentEvents = document,
    viewport = window,
  } = {}) {
    if (initialized) return;
    initialized = true;
    root = documentRoot;
    mediaQuery = systemTheme;
    viewportTarget = viewport;
    button = app.state.ui.$("#themeMenuBtn");
    menu = app.state.ui.$("#themeMenu");

    let storedPreference = "system";
    try { storedPreference = app.persistence.read(THEME_STORAGE_KEY, "system"); } catch { /* use system */ }
    applyThemePreference(storedPreference, { persist: false });

    if (button) {
      app.lifecycle.listen(button, "click", toggleThemeMenu);
      app.lifecycle.listen(button, "keydown", (event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          openThemeMenu({ focus: event.key === "ArrowDown" ? "first" : "last" });
        }
        if (event.key === "Escape") closeThemeMenu();
      });
    }
    if (menu) {
      app.lifecycle.listen(menu, "click", (event) => {
        const item = event.target.closest?.("[data-theme-preference]");
        if (!item) return;
        applyThemePreference(item.dataset.themePreference);
        closeThemeMenu({ restoreFocus: true });
      });
      app.lifecycle.listen(menu, "keydown", handleThemeMenuKeydown);
    }
    app.lifecycle.listen(documentEvents, "pointerdown", (event) => {
      if (menu?.hidden || menu?.contains(event.target) || button?.contains(event.target)) return;
      closeThemeMenu();
    });
    app.lifecycle.listen(viewport, "resize", () => closeThemeMenu());
    app.lifecycle.listen(viewport, "scroll", () => closeThemeMenu());
    app.lifecycle.listen(mediaQuery, "change", () => {
      if (app.state.ui.themePreference === "system") applyThemePreference("system", { persist: false });
    });
    app.lifecycle.listen(storageEvents, "storage", (event) => {
      if (event.key !== THEME_STORAGE_KEY) return;
      applyThemePreference(event.newValue, { persist: false });
    });
  }

  Object.assign(app.commands, {
    applyThemePreference,
    closeThemeMenu,
    initializeThemeController,
    openThemeMenu,
    positionThemeMenu,
    themeLabel,
    toggleThemeMenu,
  });
  app.state.ui.THEME_PREFERENCES = THEME_PREFERENCES;
  app.state.ui.THEME_STORAGE_KEY = THEME_STORAGE_KEY;
}
