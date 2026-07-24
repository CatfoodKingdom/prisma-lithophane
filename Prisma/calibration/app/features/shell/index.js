/** Install features/shell/index commands. */
export function installFeaturesShellIndex(app) {
  async function loadServerConfig() {
    try {
      app.state.session._serverConfig = await app.api.fetchConfig();
      app.state.session.data._stepLibraryFullPath =
        app.state.session._serverConfig.step_export_path ||
        app.state.session._serverConfig.step_library_path ||
        "";
    } catch (_) {
      /* static mode */
    }
  }

  function enableKeyboardNavigationMode() {
    document.body?.classList.add("using-keyboard-nav");
  }

  function disableKeyboardNavigationMode() {
    document.body?.classList.remove("using-keyboard-nav");
  }

  function _sbEl(id) {
    return document.getElementById(id);
  }

  function readSampleInspectExpandedPreference() {
    try {
      return (
        window.sessionStorage?.getItem(
          app.constants.SAMPLE_INSPECT_EXPANDED_SESSION_KEY,
        ) === "1"
      );
    } catch (_) {
      return false;
    }
  }

  function setSampleInspectExpandedPreference(expanded) {
    app.state.logbook._sampleInspectExpanded = !!expanded;
    try {
      window.sessionStorage?.setItem(
        app.constants.SAMPLE_INSPECT_EXPANDED_SESSION_KEY,
        app.state.logbook._sampleInspectExpanded ? "1" : "0",
      );
    } catch (_) {
      // Session storage can be unavailable in hardened browser contexts.
    }
  }

  function renderSummaryRail() {
    const exps = app.state.session.data.samples || [];
    const processedCount = exps.filter(
      (e) => e._processing_status === "processed" || e.processed,
    ).length;
    app.dom.summaryRail.innerHTML = `
      <div class="rail-stat"><strong>${(app.state.session.data.filaments || []).length}</strong><span>filaments</span></div>
      <div class="rail-stat"><strong>${exps.length}</strong><span>samples</span></div>
      <div class="rail-stat"><strong>${(app.state.session.data.steps || []).length}</strong><span>STEP files</span></div>
      <div class="rail-stat"><strong>${processedCount}</strong><span>processed strips</span></div>
    `;
  }

  function renderModeButtons() {
    if (app.dom.modeSwitch) {
      app.dom.modeSwitch.setAttribute("role", "tablist");
      app.dom.modeSwitch.setAttribute("aria-label", "Primary navigation");
    }
    app.dom.modeSwitch.querySelectorAll(".mode-button").forEach((button) => {
      const isActive = button.dataset.mode === app.state.navigation.currentMode;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", isActive ? "true" : "false");
      button.tabIndex = isActive ? 0 : -1;
    });
  }

  function getTabButtons(container, selector) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(selector)).filter(
      (button) => !button.disabled,
    );
  }

  function focusModeButton(modeId = app.state.navigation.currentMode) {
    const button = app.dom.modeSwitch?.querySelector(
      `.mode-button[data-mode="${modeId}"]`,
    );
    button?.focus();
  }

  function focusSubtabButton(subtabId = app.state.navigation.currentSubtab) {
    const button = app.dom.subtabRow?.querySelector(
      `.subtab-button[data-subtab="${subtabId}"]`,
    );
    button?.focus();
  }

  async function activateMode(modeId) {
    if (!modeId) return;
    app.state.navigation.currentMode = modeId;
    const subtabs =
      app.constants.modeConfig[app.state.navigation.currentMode]?.subtabs || [];
    app.state.navigation.currentSubtab =
      subtabs.length > 0 ? subtabs[0].id : "";
    app.commands.clearSelectionAndDrawer();
    app.commands.closeStepBuilderDrawer();
    app.commands.closeBundleMgmtDrawer();
    app.commands.closeFilamentBuilderPanel();
    if (
      app.state.navigation.currentMode === "imageProcessing" &&
      !app.state.images.importState.loaded
    ) {
      app.state.images.importState.loading = true;
      app.state.images.importState.loadingMessage = "Loading image inbox";
      app.commands.renderWorkspace();
      await app.commands.loadImportData();
    }
    app.commands.renderWorkspace();
  }

  async function activateSubtab(subtabId) {
    if (!subtabId) return;
    app.state.navigation.currentSubtab = subtabId;
    app.commands.clearSelectionAndDrawer();
    app.commands.closeStepBuilderDrawer();
    app.commands.closeBundleMgmtDrawer();
    app.commands.closeFilamentBuilderPanel();
    if (
      app.state.navigation.currentSubtab === "associate" &&
      !app.state.images.importState.loaded
    ) {
      app.state.images.importState.loading = true;
      app.state.images.importState.loadingMessage = "Loading image inbox";
      app.commands.renderWorkspace();
      await app.commands.loadImportData();
    }
    app.commands.renderWorkspace();
  }

  function bindArrowTabNavigation(container, selector, options = {}) {
    if (!container || container.dataset.arrowTabsBound === "true") return;
    container.dataset.arrowTabsBound = "true";
    app.lifecycle.listen(container, "keydown", async (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const activeButton = e.target.closest(selector);
      if (!activeButton || !container.contains(activeButton)) return;

      const buttons = app.commands.getTabButtons(container, selector);
      if (!buttons.length) return;

      const currentIndex = Math.max(0, buttons.indexOf(activeButton));
      let targetButton = null;

      if (e.key === "ArrowLeft") {
        targetButton =
          buttons[(currentIndex - 1 + buttons.length) % buttons.length];
      } else if (e.key === "ArrowRight") {
        targetButton = buttons[(currentIndex + 1) % buttons.length];
      } else if (e.key === "Home") {
        targetButton = buttons[0];
      } else if (e.key === "End") {
        targetButton = buttons[buttons.length - 1];
      } else if (
        e.key === "ArrowDown" &&
        typeof options.onArrowDown === "function"
      ) {
        e.preventDefault();
        options.onArrowDown(activeButton);
        return;
      } else if (
        e.key === "ArrowUp" &&
        typeof options.onArrowUp === "function"
      ) {
        e.preventDefault();
        options.onArrowUp(activeButton);
        return;
      } else {
        return;
      }

      if (!targetButton || targetButton === activeButton) return;

      e.preventDefault();
      if (typeof options.activate === "function") {
        await options.activate(targetButton);
      } else {
        targetButton.click();
      }
      if (typeof options.focusActive === "function") {
        options.focusActive(targetButton);
      } else {
        targetButton.focus();
      }
    });
  }

  function mountSubtabsInOwnedSurface() {
    if (!app.dom.subtabContainer) return;
    const subtabs =
      app.constants.modeConfig[app.state.navigation.currentMode]?.subtabs || [];
    const defaultContent = document.getElementById("defaultContent");
    const importView = document.getElementById("importView");
    const importModeShell = document.getElementById("importModeShell");
    const mainLogbook = defaultContent?.querySelector(".main-logbook");

    if (subtabs.length <= 1) {
      app.dom.modeSwitch.insertAdjacentElement(
        "afterend",
        app.dom.subtabContainer,
      );
      return;
    }

    if (
      app.state.navigation.currentMode === "imageProcessing" &&
      app.state.navigation.currentSubtab === "associate" &&
      importModeShell
    ) {
      importModeShell.prepend(app.dom.subtabContainer);
      return;
    }

    if (mainLogbook) {
      mainLogbook.prepend(app.dom.subtabContainer);
    }
  }

  function renderSubtabs() {
    const subtabs =
      app.constants.modeConfig[app.state.navigation.currentMode]?.subtabs || [];
    if (app.dom.subtabContainer) {
      app.dom.subtabContainer.style.display = subtabs.length <= 1 ? "none" : "";
    }
    app.dom.workspaceRoot?.classList.toggle(
      "subtabs-hidden",
      subtabs.length <= 1,
    );
    if (subtabs.length === 0) {
      app.dom.subtabRow.innerHTML = "";
      app.state.navigation.currentSubtab = "";
      return;
    }
    if (!subtabs.some((tab) => tab.id === app.state.navigation.currentSubtab)) {
      app.state.navigation.currentSubtab = subtabs[0].id;
    }

    app.dom.subtabRow.setAttribute("role", "tablist");
    app.dom.subtabRow.setAttribute(
      "aria-label",
      `${app.state.navigation.currentMode} section navigation`,
    );

    app.dom.subtabRow.innerHTML = subtabs
      .map(
        (tab) => `
      <button class="subtab-button${tab.id === app.state.navigation.currentSubtab ? " is-active" : ""}" data-subtab="${tab.id}" role="tab" aria-selected="${tab.id === app.state.navigation.currentSubtab ? "true" : "false"}" tabindex="${tab.id === app.state.navigation.currentSubtab ? "0" : "-1"}">
        ${tab.label}
      </button>
    `,
      )
      .join("");

    app.dom.subtabRow.querySelectorAll(".subtab-button").forEach((button) => {
      button.addEventListener("click", async () => {
        await app.commands.activateSubtab(button.dataset.subtab);
      });
    });
  }

  function renderStatusSummary() {
    const metas = app.state.session.data.samples.map((exp) =>
      app.commands.sampleStatusMeta(exp),
    );
    const processed = metas.filter(
      (meta) => meta.cls === "processed" || meta.cls === "accepted",
    ).length;
    const ready = metas.filter((meta) => meta.cls === "ready").length;
    const incomplete = metas.filter((meta) => meta.cls === "incomplete").length;
    const unassigned = metas.filter((meta) => meta.cls === "unassigned").length;
    const profiled = app.state.session.data.filaments.filter(
      (fil) => fil.has_profile,
    ).length;

    app.dom.statusSummary.innerHTML = `
      <div class="status-box"><span>Processed</span><strong>${processed}</strong></div>
      <div class="status-box"><span>Ready</span><strong>${ready}</strong></div>
      <div class="status-box"><span>Incomplete</span><strong>${incomplete}</strong></div>
      <div class="status-box"><span>Unassigned</span><strong>${unassigned}</strong></div>
      <div class="status-box"><span>Profiled filaments</span><strong>${profiled}</strong></div>
    `;
  }

  function syncRecordDrawerPosition() {
    if (!app.dom.recordDrawer) return;
    const importModeShell = document.getElementById("importModeShell");
    const importView = document.getElementById("importView");
    const defaultPrimaryPanel = document.querySelector(
      "#defaultContent .panel:first-child",
    );
    const importIsVisible =
      importView && !importView.classList.contains("is-hidden");
    const ownedSurface =
      app.state.navigation.currentMode === "imageProcessing" &&
      app.state.navigation.currentSubtab === "associate" &&
      importIsVisible &&
      importModeShell
        ? importModeShell
        : defaultPrimaryPanel;
    if (!ownedSurface) {
      app.dom.recordDrawer.style.removeProperty("--record-drawer-top");
      return;
    }
    const top = Math.max(
      12,
      Math.round(ownedSurface.getBoundingClientRect().top),
    );
    app.dom.recordDrawer.style.setProperty("--record-drawer-top", `${top}px`);
  }

  function getLinkedSampleDrawerMetrics() {
    if (!app.dom.recordDrawer?.classList.contains("is-open")) return null;
    const mainRect = app.dom.recordDrawer.getBoundingClientRect();
    const availableWidth = Math.floor(
      mainRect.left -
        app.constants.LINKED_SAMPLE_DRAWER_GAP -
        app.constants.LINKED_SAMPLE_DRAWER_MIN_LEFT_MARGIN,
    );
    const canOpen =
      availableWidth >= app.constants.LINKED_SAMPLE_DRAWER_MIN_WIDTH;
    const width = Math.min(
      app.constants.LINKED_SAMPLE_DRAWER_MAX_WIDTH,
      Math.max(0, availableWidth),
    );
    return {
      canOpen,
      width,
      top: Math.max(12, Math.round(mainRect.top)),
      right: Math.max(
        12,
        Math.round(
          window.innerWidth -
            mainRect.left +
            app.constants.LINKED_SAMPLE_DRAWER_GAP,
        ),
      ),
    };
  }

  function canOpenLinkedSampleDrawer() {
    return Boolean(app.commands.getLinkedSampleDrawerMetrics()?.canOpen);
  }

  function updateLinkedSampleTriggers(root = app.dom.detailSidebar) {
    if (!root) return;
    const enabled = app.commands.canOpenLinkedSampleDrawer();
    root.querySelectorAll("[data-linked-sample]").forEach((node) => {
      node.classList.toggle("is-disabled", !enabled);
      node.classList.toggle("is-enabled", enabled);
      node.setAttribute("aria-disabled", enabled ? "false" : "true");
      node.tabIndex = enabled ? 0 : -1;
    });
  }

  function syncLinkedSampleDrawerPosition() {
    if (!app.dom.linkedSampleDrawer) return;
    const metrics = app.commands.getLinkedSampleDrawerMetrics();
    if (!metrics?.canOpen) {
      if (app.dom.linkedSampleDrawer.classList.contains("is-open")) {
        app.commands.closeLinkedSampleDrawer({ restoreFocus: false });
      }
      return;
    }

    app.dom.linkedSampleDrawer.style.setProperty(
      "--record-drawer-top",
      `${metrics.top}px`,
    );
    app.dom.linkedSampleDrawer.style.setProperty(
      "--linked-drawer-shift",
      `${metrics.right}px`,
    );
    app.dom.linkedSampleDrawer.style.width = `${metrics.width}px`;
    app.dom.linkedSampleDrawer.style.right = `${metrics.right}px`;
  }

  function syncModeTabRowWidth() {
    if (!app.dom.modeSwitch) return;
    const importModeShell = document.getElementById("importModeShell");
    const importView = document.getElementById("importView");
    const defaultPrimaryPanel = document.querySelector(
      "#defaultContent .panel:first-child",
    );
    const importIsVisible =
      importView && !importView.classList.contains("is-hidden");
    let ownedSurface = defaultPrimaryPanel;
    if (
      app.state.navigation.currentMode === "imageProcessing" &&
      app.state.navigation.currentSubtab === "associate" &&
      importIsVisible &&
      importModeShell
    ) {
      ownedSurface = importModeShell;
    }
    if (!ownedSurface) {
      app.dom.modeSwitch.style.removeProperty("--mode-tab-row-width");
      return;
    }
    const rect = ownedSurface.getBoundingClientRect();
    const availableWidth = Math.max(
      240,
      Math.floor(window.innerWidth - rect.left - 12),
    );
    const width = Math.max(
      320,
      Math.min(Math.round(rect.width), availableWidth),
    );
    app.dom.modeSwitch.style.setProperty("--mode-tab-row-width", `${width}px`);
  }

  function setDrawerHeading(value, { html = false, technical = false } = {}) {
    if (!app.dom.detailHeading) return;
    app.dom.detailHeading.classList.toggle("is-technical", technical);
    if (html) {
      app.dom.detailHeading.innerHTML = value;
    } else {
      app.dom.detailHeading.textContent = value;
    }
  }

  function setDetailSidebarStackMode(mode = "default") {
    if (!app.dom.detailSidebar) return;
    app.dom.detailSidebar.classList.remove("drawer-form-stack");
    if (mode === "form") {
      app.dom.detailSidebar.classList.add("drawer-form-stack");
    }
  }

  Object.assign(app.commands, {
    loadServerConfig,
    enableKeyboardNavigationMode,
    disableKeyboardNavigationMode,
    _sbEl,
    readSampleInspectExpandedPreference,
    setSampleInspectExpandedPreference,
    renderSummaryRail,
    renderModeButtons,
    getTabButtons,
    focusModeButton,
    focusSubtabButton,
    activateMode,
    activateSubtab,
    bindArrowTabNavigation,
    mountSubtabsInOwnedSurface,
    renderSubtabs,
    renderStatusSummary,
    syncRecordDrawerPosition,
    getLinkedSampleDrawerMetrics,
    canOpenLinkedSampleDrawer,
    updateLinkedSampleTriggers,
    syncLinkedSampleDrawerPosition,
    syncModeTabRowWidth,
    setDrawerHeading,
    setDetailSidebarStackMode,
  });
}
