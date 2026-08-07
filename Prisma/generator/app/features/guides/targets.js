function staticTarget(selector, revealId) {
  return Object.freeze({ selector, reveal_id: revealId });
}

function dynamicTarget(resolve, revealId) {
  return Object.freeze({ resolve, reveal_id: revealId });
}

function groupedTarget(regions, revealId) {
  return Object.freeze({
    regions: Object.freeze(regions.map(region => Object.freeze({ ...region }))),
    reveal_id: revealId,
  });
}

function layeredTarget({ spotlightRegions, frameRegions, placementRegions = spotlightRegions }, revealId) {
  const freezeRegions = regions => Object.freeze(
    regions.map(region => Object.freeze({ ...region })),
  );
  return Object.freeze({
    spotlight_regions: freezeRegions(spotlightRegions),
    frame_regions: freezeRegions(frameRegions),
    placement_regions: freezeRegions(placementRegions),
    reveal_id: revealId,
  });
}

function context(app) {
  return app.state.guides.runtimeContext || {};
}

function cardById(root, cardId, attribute = "data-card-id") {
  if (!root || !cardId) return null;
  return [...root.querySelectorAll(`[${attribute}]`)]
    .find(element => element.getAttribute(attribute) === cardId) || null;
}

function tutorialImageCard(app) {
  const filename = context(app).tutorialImageFilename;
  const sourceRef = context(app).tutorialImageSourceRef;
  return [...(app.state.ui.$("#imageGrid")?.querySelectorAll(".image-card") || [])]
    .find(card => card.dataset.filename === filename && card.dataset.sourceRef === sourceRef) || null;
}

function structurallyEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => structurallyEqual(value, right[index]));
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every(
      (key, index) => key === rightKeys[index] && structurallyEqual(left[key], right[key]),
    );
}

const TARGETS = Object.freeze({
  "sidebar.printer": layeredTarget({
    spotlightRegions: [{ selector: '[data-guide-target="sidebar.printer"]', all: false }],
    frameRegions: [{ selector: "#printerConfigBtn", all: false }],
  }, "sidebar.printer"),
  "sidebar.active-printer": staticTarget('[data-guide-target="sidebar.active-printer"]', "sidebar.active-printer"),
  "printer.configuration": layeredTarget({
    spotlightRegions: [{ selector: '[data-guide-target="printer.configuration"]', all: false }],
    frameRegions: [{ selector: '[data-guide-target="printer.configuration-fields"]', all: false }],
  }, "printer.configuration"),
  "sidebar.active-nozzle": staticTarget('[data-guide-target="sidebar.active-nozzle"]', "sidebar.active-nozzle"),
  "sidebar.model-library": staticTarget('[data-guide-target="sidebar.model-library"]', "sidebar.model-library"),
  "sidebar.active-filaments": staticTarget('[data-guide-target="sidebar.active-filaments"]', "sidebar.active-filaments"),
  "topbar.theme": staticTarget('[data-guide-target="topbar.theme"]', "topbar.theme"),
  "topbar.clear-temp": staticTarget('[data-guide-target="topbar.clear-temp"]', "topbar.clear-temp"),
  "topbar.help-guides": staticTarget('[data-guide-target="topbar.help-guides"]', "topbar.help-guides"),
  "topbar.settings": staticTarget('[data-guide-target="topbar.settings"]', "topbar.settings"),
  "topbar.solve": staticTarget("#startSolveBtn", "topbar.solve"),
  "workflow.tabs": staticTarget('[data-guide-target="workflow.tabs"]', "workflow.tabs"),
  "workflow.image": staticTarget('[data-guide-target="workflow.image"]', "workflow.image-page"),
  "workflow.overview": groupedTarget([
    { selector: '[data-guide-target="workflow.tabs"]', all: false },
    {
      selector: '#solveActionSplit, [data-guide-target="topbar.settings"]',
      all: true,
    },
  ], "workflow.overview"),
  "workflow.palette": staticTarget('.mode-button[data-tab="creation"]', "workflow.palette"),
  "workflow.preview": staticTarget('.mode-button[data-tab="solve"]', "workflow.preview"),
  "workflow.export": staticTarget('.mode-button[data-tab="export"]', "workflow.export"),
  "image.library": staticTarget('[data-guide-target="image.library"]', "image.library"),
  "image.library-import": groupedTarget([
    { selector: "#imageGrid", all: false },
    { selector: ".upload-btn, #imageLibraryOpenFolderBtn", all: true },
  ], "image.library-import"),
  "image.library-maintenance": groupedTarget([
    { selector: "#imageGrid", all: false },
    { selector: "#imageLibraryRefreshBtn, #libraryResizeBtn", all: true },
  ], "image.library-maintenance"),
  "image.preview": staticTarget("#imagePreviewPane", "image.preview"),
  "image.adjustments": staticTarget(".framing-editor", "image.adjustments"),
  "image.preview-adjustments": layeredTarget({
    spotlightRegions: [
      { selector: "#imagePreviewPane", all: false },
      { selector: ".framing-editor", all: false },
    ],
    frameRegions: [
      { selector: "#imagePreviewPane", all: false },
      { selector: ".framing-editor", all: false },
    ],
    placementRegions: [
      { selector: ".framing-editor", all: false },
    ],
  }, "image.preview-adjustments"),
  "image.aspect-experiment": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: "#frameControlsSize .ctrl-section-grid2", all: false }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.aspect-experiment"),
  "image.aspect-controls": staticTarget("#frameControlsSize", "image.aspect-controls"),
  "image.framing": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [
      { selector: "#frameCanvasWrap", all: false },
      { selector: '[data-guide-target-part="image.transform-controls"]', all: true },
    ],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.framing"),
  "image.adjustment-image-tab": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: '[data-guide-target="image.adjustment-image-tab"]', all: false }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.adjustment-image-tab"),
  "image.appearance": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: "#frameControlsImage", all: false }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.appearance"),
  "image.border": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: '[data-guide-target-part="image.border-controls"]', all: true }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.border"),
  "image.reset-framing": layeredTarget({
    spotlightRegions: [
      { selector: '[data-guide-target-part="image.canvas-reset"]', all: true },
      { selector: "#borderToggle", all: false },
    ],
    frameRegions: [
      { selector: '[data-guide-target-part="image.canvas-reset"]', all: true },
      { selector: "#borderToggle", all: false },
    ],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.reset-framing"),
  "image.crop-fit": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: "#fitImageBtn, #fillWidthBtn, #fillHeightBtn", all: true }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.crop-fit"),
  "image.physical-dimensions": layeredTarget({
    spotlightRegions: [{ selector: ".framing-editor", all: false }],
    frameRegions: [{ selector: '[data-guide-target-part="image.canvas-dimensions"]', all: true }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.physical-dimensions"),
  "image.tutorial-canvas": layeredTarget({
    spotlightRegions: [{ selector: '[data-guide-target-part="image.canvas-dimensions"]', all: true }],
    frameRegions: [{ selector: '[data-guide-target-part="image.canvas-dimensions"]', all: true }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.tutorial-canvas"),
  "image.canvas-settings": staticTarget("#frameControlsSize", "image.canvas-settings"),
  "image.summary": layeredTarget({
    spotlightRegions: [
      { selector: ".framing-editor", all: false },
      { selector: "#imageInfoGrid", all: false },
    ],
    frameRegions: [{ selector: "#imageInfoGrid", all: false }],
    placementRegions: [{ selector: ".framing-editor", all: false }],
  }, "image.summary"),
  "basics.tutorial-image": layeredTarget({
    spotlightRegions: [{ selector: "#imageLibraryPanel", all: false }],
    frameRegions: [{ resolve: tutorialImageCard }],
  }, "basics.tutorial-image"),
  "palette.modes": staticTarget(".creation-mode-tabs", "palette.modes"),
  "palette.autosuggest-overview": groupedTarget([
    { selector: ".creation-mode-tabs", all: false },
    { selector: "#candidateGrid", all: false },
  ], "palette.autosuggest-overview"),
  "palette.autosuggest-controls": layeredTarget({
    spotlightRegions: [
      { selector: ".creation-controls-pane", all: false },
    ],
    frameRegions: [
      { selector: ".creation-controls-pane .suggest-field", all: true },
    ],
    placementRegions: [
      { selector: ".creation-controls-pane", all: false },
    ],
  }, "palette.autosuggest-controls"),
  "palette.candidates": staticTarget("#candidateGrid", "palette.candidates"),
  "palette.suggest": staticTarget("#suggestPalettesBtn", "palette.suggest"),
  "palette.suggestions": staticTarget("#deckCards", "palette.suggestions"),
  "palette.deck": layeredTarget({
    spotlightRegions: [{ selector: "#railDeck", all: false }],
    frameRegions: [],
  }, "palette.deck"),
  "palette.manual": groupedTarget([
    { selector: "#panelManualBuilder", all: false },
    { selector: "#manualPalettePanel", all: false },
  ], "palette.manual"),
  "basics.manual-card": dynamicTarget(
    app => cardById(app.state.ui.$("#railDeckList"), context(app).manualCardId),
    "basics.manual-card",
  ),
  "basics.variant-card": dynamicTarget(
    app => cardById(app.state.ui.$("#railDeckList"), context(app).variantCardId),
    "basics.variant-card",
  ),
  "basics.palette-a": dynamicTarget(
    app => cardById(app.state.ui.$("#railDeckList"), context(app).paletteA?.id),
    "basics.palette-a",
  ),
  "basics.palette-b": dynamicTarget(
    app => cardById(app.state.ui.$("#railDeckList"), context(app).paletteB?.id),
    "basics.palette-b",
  ),
  "settings.drawer": staticTarget("#settingsDrawer", "settings.drawer"),
  "settings.drawer-overview": layeredTarget({
    spotlightRegions: [{ selector: "#settingsDrawer", all: false }],
    frameRegions: [],
  }, "settings.drawer-overview"),
  "settings.essentials-resolution": layeredTarget({
    spotlightRegions: [{ selector: '[data-settings-group="geometry"]', all: false }],
    frameRegions: [{ selector: '[data-guide-target-part="settings.essentials-resolution"]', all: true }],
  }, "settings.essentials-resolution"),
  "settings.essentials-construction": layeredTarget({
    spotlightRegions: [{ selector: '[data-settings-group="geometry"]', all: false }],
    frameRegions: [{ selector: '[data-guide-target-part="settings.essentials-construction"]', all: true }],
  }, "settings.essentials-construction"),
  "settings.profiles": staticTarget(
    '[data-guide-target="settings.profiles"]',
    "settings.profiles",
  ),
  "settings.advanced": groupedTarget([
    { selector: "#settingsAdvancedToggle", all: false },
    { selector: "#settingsDrawerBody", all: false },
  ], "settings.advanced"),
  "settings.profile-essentials": groupedTarget([
    { selector: ".settings-profile-bar", all: false },
    { selector: '[data-settings-group="geometry"]', all: false },
  ], "settings.profile-essentials"),
  "settings.preprocessing-solver": groupedTarget([
    { selector: '[data-settings-group="preprocessing"]', all: false },
    { selector: '[data-settings-group="solver"]', all: false },
  ], "settings.preprocessing-solver"),
  "settings.white-cap": groupedTarget([
    { selector: '[data-guide-target-part="settings.white-cap.overview"]', all: true },
    { selector: '[data-guide-target-part="settings.white-cap.boundary"]', all: true },
    { selector: '[data-guide-target-part="settings.white-cap.detail"]', all: true },
  ], "settings.white-cap"),
  "settings.white-point-rescale": staticTarget(
    '[data-guide-target="settings.white-point-rescale"]',
    "settings.white-point-rescale",
  ),
  "preview.views": groupedTarget([
    { selector: "#solveViewBar", all: false },
    { selector: "#solveComparisonGrid", all: false },
  ], "preview.views"),
  "preview.comparison": staticTarget("#solveComparisonGrid", "preview.comparison"),
  "preview.history": staticTarget("#solveRunCards", "preview.history"),
  "preview.overview": groupedTarget([
    { selector: "#solveComparisonGrid", all: false },
    { selector: "#solveRunCards", all: false },
  ], "preview.overview"),
  "export.options": staticTarget(".export-command-grid", "export.options"),
  "export.generate": staticTarget("#exportFilesBtn", "export.generate"),
  "export.results": staticTarget(".export-results-panel", "export.results"),
  "basics.export-run-a": dynamicTarget(
    app => cardById(app.state.ui.$("#exportRunCards"), context(app).runAId, "data-export-run-id"),
    "basics.export-run-a",
  ),
  "saving.palette-card": dynamicTarget(
    app => app.state.ui.$("#paletteSaveModal[aria-hidden=\"false\"]")
      || (!app.state.ui.$("#deckCardMenu")?.hidden ? app.state.ui.$("#deckCardMenu") : null)
      || cardById(app.state.ui.$("#railDeckList"), context(app).tutorialPalettes?.[0]?.id),
    "saving.palette-card",
  ),
  "saving.palette-load": dynamicTarget(
    app => app.state.ui.$(".load-palette-popover") || app.state.ui.$("#railLoadPaletteBtn"),
    "saving.palette-load",
  ),
  "saving.palette-clear": staticTarget("#railClearDeckBtn", "saving.palette-clear"),
  "saving.settings-thickness": groupedTarget([
    { selector: ".settings-profile-bar", all: false },
    { selector: "#cfgTMax", all: false },
  ], "saving.settings-thickness"),
  "saving.settings-save": dynamicTarget(
    app => app.state.ui.$("#settingsProfileSaveModal[aria-hidden=\"false\"]")
      || app.state.ui.$("#appDialog[aria-hidden=\"false\"]")
      || app.state.ui.$("#settingsProfileSaveBtn"),
    "saving.settings-save",
  ),
  "saving.settings-profiles": dynamicTarget(
    app => app.state.ui.$("#settingsProfileModal[aria-hidden=\"false\"]")
      || app.state.ui.$("#settingsProfileBrowseBtn"),
    "saving.settings-profiles",
  ),
  "saving.settings-profiles-reopen": dynamicTarget(
    app => app.state.ui.$("#settingsProfileModal[aria-hidden=\"false\"]")
      || app.state.ui.$("#settingsProfileBrowseBtn"),
    "saving.settings-profiles-reopen",
  ),
  "saving.solve": dynamicTarget(
    app => app.state.ui.$("#opProgress:not(.is-hidden)") || app.state.ui.$("#startSolveBtn"),
    "saving.solve",
  ),
  "saving.run-card": dynamicTarget(
    app => app.state.ui.$("#appDialog[aria-hidden=\"false\"]")
      || cardById(app.state.ui.$("#solveRunCards"), context(app).loadedRunId || context(app).guideRunId, "data-run-id")
      || app.state.ui.$("#solveRunCards .solve-run-card"),
    "saving.run-card",
  ),
  "saving.history-clear": staticTarget("#clearSolveHistoryBtn", "saving.history-clear"),
  "saving.saved-runs": dynamicTarget(
    app => app.state.ui.$("#savedRunsModal[aria-hidden=\"false\"]") || app.state.ui.$("#savedRunsBtn"),
    "saving.saved-runs",
  ),
  "saving.saved-runs-reopen": dynamicTarget(
    app => app.state.ui.$("#savedRunsModal[aria-hidden=\"false\"]") || app.state.ui.$("#savedRunsBtn"),
    "saving.saved-runs-reopen",
  ),
  "saving.run-settings": dynamicTarget(
    app => app.state.solve.solveRunSettingsPanelEl || app.state.ui.$("#solveRunCards .solve-run-settings-btn"),
    "saving.run-settings",
  ),
  "saving.export": staticTarget("#tabExport", "saving.export"),
});

const KNOWN_GUIDE_PREDICATES = new Set([
  "settings.drawer-open",
  "settings.drawer-closed",
  "printer-config.open",
  "printer-config.closed",
  "basics.tutorial-printer-active",
  "basics.tutorial-nozzle-active",
  "saving.prepared",
  "saving.palette-saved",
  "saving.palette-removed",
  "saving.palette-loaded",
  "saving.palette-save-deleted",
  "saving.settings-2.6-modified",
  "saving.settings-profile-saved",
  "saving.settings-3.0-modified",
  "saving.settings-browser-open",
  "saving.basic-restored",
  "saving.settings-profile-deleted",
  "saving.solve-complete",
  "saving.run-saved",
  "saving.history-cleared",
  "saving.deck-cleared",
  "saving.run-loaded",
  "saving.run-settings-open",
  "saving.temp-loaded",
  "saving.run-save-deleted",
  "basics.tutorial-image-selected",
  "basics.image-reset",
  "basics.canvas-ready",
  "image.adjustment-tab-image",
  "tab.creation",
  "tab.export",
  "basics.two-suggestions-ready",
  "basics.tutorial-palettes-in-deck",
  "basics.manual-card-added",
  "basics.manual-card-removed",
  "basics.palette-variant-started",
  "basics.palette-variant-added",
  "basics.palette-variant-removed",
  "basics.palette-a-active",
  "basics.palette-b-active",
  "basics.tutorial-profile-ready",
  "basics.advanced-viewed-and-off",
  "basics.settings-detour-ready",
  "basics.first-solve-complete",
  "basics.second-solve-complete",
  "basics.both-runs-selected",
  "basics.export-run-a-selected",
  "basics.export-options-ready",
  "basics.export-complete",
]);

const KNOWN_GUIDE_REVEALS = new Set([
  "workflow.image-page",
  "saving.palette-page",
  "saving.preview-page",
  "saving.settings-drawer",
]);

/** Install stable guide-target resolution, runtime capture, and presentation-only reveals. */
export function installFeaturesGuidesTargets(app) {
  function resolveRegionSpecs(regionSpecs) {
    const regions = regionSpecs.map((region) => {
      if (typeof region.resolve === "function") {
        const resolved = region.resolve(app);
        return (Array.isArray(resolved) ? resolved : [resolved]).filter(Boolean);
      }
      const elements = region.all
        ? [...app.state.ui.$$(region.selector)]
        : [app.state.ui.$(region.selector)];
      return elements.filter(Boolean);
    });
    return regions.every(region => region.length > 0) ? regions : null;
  }

  function resolveGuideTargetLayout(targetId) {
    const descriptor = TARGETS[targetId];
    if (!descriptor) return null;
    if (descriptor.spotlight_regions || descriptor.frame_regions || descriptor.placement_regions) {
      const spotlightRegions = resolveRegionSpecs(descriptor.spotlight_regions || []);
      const frameRegions = resolveRegionSpecs(descriptor.frame_regions || []);
      const placementRegions = resolveRegionSpecs(
        descriptor.placement_regions || descriptor.spotlight_regions || [],
      );
      if (!spotlightRegions || !frameRegions || !placementRegions) return null;
      return { spotlightRegions, frameRegions, placementRegions };
    }
    if (descriptor.regions) {
      const regions = resolveRegionSpecs(descriptor.regions);
      return regions
        ? { spotlightRegions: regions, frameRegions: regions, placementRegions: regions }
        : null;
    }
    const target = descriptor.resolve
      ? descriptor.resolve(app)
      : app.state.ui.$(descriptor.selector);
    const regions = target ? [[target]] : null;
    return regions
      ? { spotlightRegions: regions, frameRegions: regions, placementRegions: regions }
      : null;
  }

  function resolveGuideTargetRegions(targetId) {
    return resolveGuideTargetLayout(targetId)?.spotlightRegions || null;
  }

  function resolveGuideTarget(targetId) {
    const layout = resolveGuideTargetLayout(targetId);
    return layout?.frameRegions?.[0]?.[0] || layout?.spotlightRegions?.[0]?.[0] || null;
  }

  function captureGuidePresentation() {
    return {
      currentTab: app.state.ui.currentTab || "image",
      settingsDrawerOpen: !!app.state.settings.settingsDrawerOpen,
      settingsAdvancedVisible: !!app.state.settings.settingsAdvancedVisible,
      frameEditorTab: app.state.image.frameEditorTab || "size",
      creationMode: app.state.palette.creationMode || "auto",
      solveView: app.state.solve.solveView || "predicted",
      solveWhiteCapView: app.state.solve.solveWhiteCapView,
      solveColorRegionsView: app.state.solve.solveColorRegionsView,
      solveAdvancedViewsOpen: !!app.state.solve.solveAdvancedViewsOpen,
      settingsScrollTop: app.state.ui.$("#settingsDrawerBody")?.scrollTop || 0,
      tabSwitchScrollLeft: app.state.ui.$("#tabSwitch")?.scrollLeft || 0,
      tabContentScrollTop: app.state.ui.$(".tab-content-area")?.scrollTop || 0,
    };
  }

  function switchTo(tab) {
    if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
    if (app.state.ui.currentTab !== tab) app.commands.switchTab(tab);
  }

  function revealGuideTarget(revealId, { reviewOnly = false } = {}) {
    if (revealId === "saving.palette-page") {
      app.commands.switchTab("creation");
      return;
    }
    if (revealId === "saving.preview-page") {
      app.commands.closeSettingsProfileBrowserModal?.();
      app.commands.closeSettingsDrawer?.();
      app.commands._setSavedRunsModalOpen?.(false);
      app.commands.hideSolveRunSettingsPanel?.();
      app.commands.switchTab("solve");
      return;
    }
    if (revealId === "saving.settings-drawer") {
      app.commands._setSavedRunsModalOpen?.(false);
      app.commands.hideSolveRunSettingsPanel?.();
      app.commands.openSettingsDrawer?.();
      return;
    }
    if (revealId === "saving.settings-profiles-reopen") {
      app.commands.openSettingsDrawer?.();
      if (!reviewOnly && app.state.ui.$("#settingsProfileModal")?.getAttribute("aria-hidden") !== "false") {
        void app.commands.handleSettingsProfilesBrowse?.();
      }
      return;
    }
    if (revealId === "saving.saved-runs-reopen") {
      app.commands.closeSettingsDrawer?.();
      app.commands.switchTab("solve");
      if (!reviewOnly && app.state.ui.$("#savedRunsModal")?.getAttribute("aria-hidden") !== "false") {
        void app.commands.openSavedRunsModal?.("run");
      }
      return;
    }
    if (revealId.startsWith("saving.palette")) {
      app.commands.closeSettingsProfileBrowserModal?.();
      app.commands._setSavedRunsModalOpen?.(false);
      app.commands.hideSolveRunSettingsPanel?.();
      app.commands.switchTab("creation");
      return;
    }
    if (revealId.startsWith("saving.settings")) {
      app.commands._setSavedRunsModalOpen?.(false);
      app.commands.hideSolveRunSettingsPanel?.();
      app.commands.openSettingsDrawer?.();
      return;
    }
    if (["saving.solve", "saving.run-card", "saving.history-clear", "saving.saved-runs", "saving.run-settings"].includes(revealId)) {
      if (!reviewOnly) app.commands.closeSettingsProfileBrowserModal?.();
      app.commands.switchTab("solve");
      return;
    }
    if (revealId === "saving.export") {
      app.commands.closeSettingsProfileBrowserModal?.();
      app.commands._setSavedRunsModalOpen?.(false);
      app.commands.hideSolveRunSettingsPanel?.();
      app.commands.switchTab("export");
      return;
    }
    if (revealId === "workflow.image-page") {
      switchTo("image");
      return;
    }
    if (revealId === "printer.configuration") {
      if (reviewOnly) return;
      const page = app.state.ui.$("#printerConfigPage");
      if (page?.classList.contains("is-hidden")) app.commands.showPrinterConfigPage();
      return;
    }
    if (revealId === "topbar.settings") {
      if (!reviewOnly) {
        if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
        app.state.settings.settingsAdvancedVisible = false;
        app.commands.updateAdvancedSettingsVisibility?.();
      }
      return;
    }
    if (revealId.startsWith("settings.")) {
      if (!reviewOnly && !app.state.settings.settingsDrawerOpen) app.commands.openSettingsDrawer();
      return;
    }
    if (
      revealId.startsWith("image.")
      || revealId === "basics.tutorial-image"
    ) {
      switchTo("image");
      if (revealId === "image.appearance") app.commands.switchFrameEditorTab("image");
      else if ([
        "image.border",
        "image.reset-framing",
        "image.canvas-settings",
        "image.tutorial-canvas",
        "image.aspect-controls",
        "image.aspect-experiment",
        "image.crop-fit",
        "image.physical-dimensions",
      ].includes(revealId)) {
        app.commands.switchFrameEditorTab("size");
      }
      return;
    }
    if (
      revealId.startsWith("palette.")
      || revealId === "basics.palette-a"
      || revealId === "basics.palette-b"
      || revealId === "basics.manual-card"
      || revealId === "basics.variant-card"
    ) {
      if ([
        "palette.deck", "basics.palette-a", "basics.palette-b", "basics.manual-card",
        "basics.variant-card",
      ].includes(revealId)) {
        if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
        return;
      }
      switchTo("creation");
      if (revealId === "palette.manual") app.commands.toggleCreationMode("manual");
      else app.commands.toggleCreationMode("auto");
      return;
    }
    if (revealId.startsWith("preview.")) {
      switchTo("solve");
      return;
    }
    if (revealId.startsWith("export.") || revealId === "basics.export-run-a") {
      switchTo("export");
    }
  }

  function restoreGuidePresentation(snapshot) {
    if (!snapshot) return;
    if (app.state.ui.currentTab !== snapshot.currentTab) app.commands.switchTab(snapshot.currentTab);
    if (snapshot.settingsDrawerOpen && !app.state.settings.settingsDrawerOpen) {
      app.commands.openSettingsDrawer();
    } else if (!snapshot.settingsDrawerOpen && app.state.settings.settingsDrawerOpen) {
      app.commands.closeSettingsDrawer();
    }
    if (
      snapshot.settingsAdvancedVisible != null
      && app.state.settings.settingsAdvancedVisible !== snapshot.settingsAdvancedVisible
    ) {
      app.state.settings.settingsAdvancedVisible = snapshot.settingsAdvancedVisible;
      app.commands.saveSettingsAdvancedVisible?.(snapshot.settingsAdvancedVisible);
      app.commands.updateAdvancedSettingsVisibility?.();
      app.commands.distributeSettingsColumns?.();
    }
    if (snapshot.frameEditorTab && app.state.image.frameEditorTab !== snapshot.frameEditorTab) {
      app.commands.switchFrameEditorTab?.(snapshot.frameEditorTab);
    }
    if (snapshot.creationMode && app.state.palette.creationMode !== snapshot.creationMode) {
      app.commands.toggleCreationMode?.(snapshot.creationMode);
    }
    let solvePresentationChanged = false;
    for (const [key, value] of [
      ["solveView", snapshot.solveView],
      ["solveWhiteCapView", snapshot.solveWhiteCapView],
      ["solveColorRegionsView", snapshot.solveColorRegionsView],
    ]) {
      if (value != null && app.state.solve[key] !== value) {
        app.state.solve[key] = value;
        solvePresentationChanged = true;
      }
    }
    if (
      snapshot.solveAdvancedViewsOpen != null
      && app.state.solve.solveAdvancedViewsOpen !== snapshot.solveAdvancedViewsOpen
    ) {
      app.commands.setSolveAdvancedViewsOpen?.(snapshot.solveAdvancedViewsOpen);
      solvePresentationChanged = true;
    }
    if (solvePresentationChanged) app.commands.renderSolveComparisonGrid?.();
    const restoreScroll = () => {
      const drawerBody = app.state.ui.$("#settingsDrawerBody");
      const tabSwitch = app.state.ui.$("#tabSwitch");
      const tabContent = app.state.ui.$(".tab-content-area");
      if (drawerBody) drawerBody.scrollTop = snapshot.settingsScrollTop;
      if (tabSwitch) tabSwitch.scrollLeft = snapshot.tabSwitchScrollLeft;
      if (tabContent) tabContent.scrollTop = snapshot.tabContentScrollTop;
    };
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => requestAnimationFrame(restoreScroll));
    } else {
      restoreScroll();
    }
  }

  function settingsAreReset() {
    const adjustment = app.state.image.imageAdjust || {};
    const frame = app.state.image.frameState || {};
    return frame.scale === 100
      && frame.rotation === 0
      && frame.panX === 0
      && frame.panY === 0
      && !frame.flipH
      && !frame.flipV
      && adjustment.mode === "color"
      && ["exposure", "contrast", "highlight", "shadow", "tint_hue", "tint_strength", "saturation", "temperature"]
        .every(key => Number(adjustment[key] || 0) === 0);
  }

  function tutorialImageIsReset() {
    const runtime = context(app);
    const frame = app.state.image.frameState || {};
    return app.state.image.selectedImage?.filename === runtime.tutorialImageFilename
      && app.state.image.selectedImage?.source_ref === runtime.tutorialImageSourceRef
      && frame.arMode === "image"
      && Math.abs(Number(frame.widthMm) - 120) < 0.001
      && Math.abs(Number(frame.heightMm) - 160) < 0.001
      && !app.state.settings.config.border
      && settingsAreReset();
  }

  function tutorialPrinterReady({ requireNozzle = false } = {}) {
    const runtime = context(app);
    const printersData = app.state.session.printersData;
    const profile = printersData?.printers?.find(printer => printer.id === "tutorial-printer");
    return printersData?.active_printer_id === "tutorial-printer"
      && structurallyEqual(profile, runtime.tutorialPrinterProfile)
      && (!requireNozzle || Math.abs(Number(printersData.active_nozzle_size) - 0.4) < 1e-6);
  }

  function guidePredicateSatisfied(predicateId) {
    const runtime = context(app);
    if (predicateId === "settings.drawer-open") {
      return !!app.state.settings.settingsDrawerOpen
        && app.state.ui.$("#settingsDrawer")?.getAttribute("aria-hidden") === "false";
    }
    if (predicateId === "settings.drawer-closed") {
      return !app.state.settings.settingsDrawerOpen
        && app.state.ui.$("#settingsDrawer")?.getAttribute("aria-hidden") === "true";
    }
    if (predicateId === "printer-config.open") {
      const page = app.state.ui.$("#printerConfigPage");
      return !!page && !page.classList.contains("is-hidden");
    }
    if (predicateId === "printer-config.closed") {
      const page = app.state.ui.$("#printerConfigPage");
      return !!page && page.classList.contains("is-hidden");
    }
    if (predicateId === "basics.tutorial-printer-active") {
      return tutorialPrinterReady()
        && app.state.ui.$("#printerConfigPage")?.classList.contains("is-hidden");
    }
    if (predicateId === "basics.tutorial-nozzle-active") {
      return tutorialPrinterReady({ requireNozzle: true });
    }
    if (predicateId === "basics.tutorial-image-selected") {
      return app.state.image.selectedImage?.filename === runtime.tutorialImageFilename
        && app.state.image.selectedImage?.source_ref === runtime.tutorialImageSourceRef;
    }
    if (predicateId === "basics.image-reset") {
      return tutorialImageIsReset();
    }
    if (predicateId === "basics.canvas-ready") {
      const frame = app.state.image.frameState || {};
      return app.state.image.selectedImage?.filename === runtime.tutorialImageFilename
        && app.state.image.selectedImage?.source_ref === runtime.tutorialImageSourceRef
        && tutorialPrinterReady({ requireNozzle: true })
        && ["specified", "image"].includes(frame.arMode)
        && Math.abs(Number(frame.widthMm) - 90) < 0.001
        && Math.abs(Number(frame.heightMm) - 120) < 0.001
        && !app.state.settings.config.border
        && settingsAreReset();
    }
    if (predicateId === "image.adjustment-tab-image") {
      return app.state.image.frameEditorTab === "image";
    }
    if (predicateId === "tab.creation") return app.state.ui.currentTab === "creation";
    if (predicateId === "tab.export") return app.state.ui.currentTab === "export";
    if (predicateId === "basics.two-suggestions-ready") {
      const paletteColors = Number(app.state.ui.$("#targetFilamentCount")?.value);
      const suggestions = Number(app.state.ui.$("#targetSuggestCount")?.value);
      const capturedCards = [runtime.paletteA, runtime.paletteB].map(captured => (
        app.state.palette.stagingDeck.find(card => card.id === captured?.id)
      ));
      return paletteColors === 3
        && suggestions >= 2
        && capturedCards.every(card => Array.isArray(card?.filament_ids) && card.filament_ids.length === 3);
    }
    if (predicateId === "basics.tutorial-palettes-in-deck") {
      const ids = new Set(app.state.palette.deck.map(card => card.id));
      return ids.has(runtime.paletteA?.id) && ids.has(runtime.paletteB?.id);
    }
    if (predicateId === "basics.manual-card-added") {
      return !!runtime.manualCardId
        && app.state.palette.deck.some(card => card.id === runtime.manualCardId)
        && guidePredicateSatisfied("basics.tutorial-palettes-in-deck");
    }
    if (predicateId === "basics.manual-card-removed") {
      return !!runtime.manualCardId
        && runtime.manualCardRemoved === true
        && !app.state.palette.deck.some(card => card.id === runtime.manualCardId)
        && guidePredicateSatisfied("basics.tutorial-palettes-in-deck");
    }
    if (predicateId === "basics.palette-variant-started") {
      return app.state.palette.manualVariantDraft?.sourceCardId === runtime.paletteA?.id
        && app.state.palette.creationMode === "manual";
    }
    if (predicateId === "basics.palette-variant-added") {
      return !!runtime.variantCardId
        && app.state.palette.activeDeckId === runtime.variantCardId
        && app.state.palette.deck.some(card => card.id === runtime.variantCardId)
        && guidePredicateSatisfied("basics.tutorial-palettes-in-deck");
    }
    if (predicateId === "basics.palette-variant-removed") {
      return !!runtime.variantCardId
        && runtime.variantCardRemoved === true
        && !app.state.palette.deck.some(card => card.id === runtime.variantCardId)
        && guidePredicateSatisfied("basics.tutorial-palettes-in-deck");
    }
    if (predicateId === "basics.palette-a-active") {
      return guidePredicateSatisfied("basics.tutorial-palettes-in-deck")
        && app.state.palette.activeDeckId === runtime.paletteA?.id;
    }
    if (predicateId === "basics.palette-b-active") {
      return guidePredicateSatisfied("basics.tutorial-palettes-in-deck")
        && app.state.palette.activeDeckId === runtime.paletteB?.id;
    }
    if (predicateId === "basics.tutorial-profile-ready") {
      const expectedProfileId = app.state.guides.currentGuide?.id
        ? `temporary-guide-${app.state.guides.currentGuide.id}`
        : null;
      return !!expectedProfileId
        && app.state.settings.loadedProfileRef?.id === expectedProfileId
        && !!runtime.tutorialSettingsSnapshot
        && structurallyEqual(
          app.commands._configSettingsProfileSnapshot(),
          runtime.tutorialSettingsSnapshot,
        );
    }
    if (predicateId === "basics.advanced-viewed-and-off") {
      return runtime.advancedSeenOn === true && !app.state.settings.settingsAdvancedVisible;
    }
    if (predicateId === "basics.settings-detour-ready") {
      return runtime.advancedSeenOn === true
        && !app.state.settings.settingsAdvancedVisible
        && guidePredicateSatisfied("basics.tutorial-profile-ready");
    }
    if (predicateId === "basics.first-solve-complete") {
      return !!runtime.runAId
        && tutorialPrinterReady({ requireNozzle: true })
        && app.state.solve.solveRuns.some(run => run.id === runtime.runAId && !!run.results);
    }
    if (predicateId === "basics.second-solve-complete") {
      return !!runtime.runBId
        && tutorialPrinterReady({ requireNozzle: true })
        && app.state.solve.solveRuns.some(run => run.id === runtime.runBId && !!run.results);
    }
    if (predicateId === "basics.both-runs-selected") {
      return !!runtime.runAId && !!runtime.runBId
        && app.state.solve.selectedRunIds.size === 2
        && app.state.solve.selectedRunIds.has(runtime.runAId)
        && app.state.solve.selectedRunIds.has(runtime.runBId);
    }
    if (predicateId === "basics.export-run-a-selected") {
      return !!runtime.runAId && app.state.export.exportSelectedRunId === runtime.runAId;
    }
    if (predicateId === "basics.export-options-ready") {
      return app.state.ui.$("#exportOutputFormat")?.value === "3mf"
        && app.state.ui.$("#exportGeometrySource")?.value === "field_derived"
        && app.state.ui.$("#exportFieldScale")?.value === "4";
    }
    if (predicateId === "basics.export-complete") {
      const run = app.state.solve.solveRuns.find(candidate => candidate.id === runtime.runAId);
      return !!runtime.exportId
        && app.commands.getRunExportRecords(run).some(record => record.id === runtime.exportId);
    }
    const primary = runtime.tutorialPalettes?.[0];
    const support = runtime.tutorialPalettes?.[1];
    const basicId = app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
    if (predicateId === "saving.prepared") {
      const selectedFilaments = new Set(app.state.palette.enabledFilaments);
      const requiredFilaments = new Set(runtime.requiredFilaments || []);
      const frame = app.state.image.frameState || {};
      return app.state.settings.loadedProfileRef?.id === basicId
        && !app.commands.isSettingsProfileModified()
        && Math.abs(Number(app.state.settings.config.t_max) - 3) < 1e-6
        && app.state.settings.config.luminance_mode === "standard"
        && app.state.settings.config.base_filament === "bambu-tough-white"
        && app.state.session.printersData?.active_printer_id === "tutorial-printer"
        && Math.abs(Number(app.state.session.printersData?.active_nozzle_size) - 0.2) < 1e-6
        && app.state.image.selectedImage?.source_ref === runtime.tutorialImage?.source_ref
        && Math.abs(Number(frame.widthMm) - 30) < 1e-6
        && Math.abs(Number(frame.heightMm) - 40) < 1e-6
        && selectedFilaments.size === requiredFilaments.size
        && [...requiredFilaments].every(id => selectedFilaments.has(id))
        && !!primary && !!support && app.state.palette.activeDeckId === primary.id
        && app.state.palette.deck.length === 2
        && structurallyEqual(app.state.palette.deck.map(card => card.filament_ids), [primary.filament_ids, support.filament_ids]);
    }
    if (predicateId === "saving.palette-saved") {
      const card = app.state.palette.deck.find(item => item.id === primary?.id);
      return !!runtime.savedPaletteId && card?.saved_source_id === runtime.savedPaletteId && card.saved === true;
    }
    if (predicateId === "saving.palette-removed") {
      return !!runtime.savedPaletteId
        && !app.state.palette.deck.some(item => item.id === primary?.id)
        && app.state.palette.savedPalettesData?.palettes?.some(item => item.id === runtime.savedPaletteId);
    }
    if (predicateId === "saving.palette-loaded") {
      const card = app.state.palette.deck.find(item => item.id === runtime.loadedPaletteId);
      return !!card && card.saved_source_id === runtime.savedPaletteId && app.state.palette.activeDeckId === card.id;
    }
    if (predicateId === "saving.palette-save-deleted") {
      const card = app.state.palette.deck.find(item => item.id === runtime.loadedPaletteId);
      return !!runtime.savedPaletteDeleted && !!card && !card.saved_source_id && card.saved !== true;
    }
    if (predicateId === "saving.settings-2.6-modified") {
      return app.state.settings.loadedProfileRef?.id === basicId
        && Math.abs(Number(app.state.settings.config.t_max) - 2.6) < 1e-6
        && app.commands.isSettingsProfileModified();
    }
    if (predicateId === "saving.settings-profile-saved") {
      const saved = app.commands.findSettingsProfile(runtime.savedProfileId);
      return !!runtime.savedProfileId
        && saved?.name === runtime.names?.savedProfileName
        && Math.abs(Number(saved?.settings?.t_max) - 2.6) < 1e-6
        && app.state.settings.loadedProfileRef?.id === runtime.savedProfileId
        && !app.commands.isSettingsProfileModified();
    }
    if (predicateId === "saving.settings-3.0-modified") {
      return app.state.settings.loadedProfileRef?.id === runtime.savedProfileId
        && Math.abs(Number(app.state.settings.config.t_max) - 3) < 1e-6
        && app.commands.isSettingsProfileModified();
    }
    if (predicateId === "saving.settings-browser-open") {
      const modal = app.state.ui.$("#settingsProfileModal");
      const selected = modal?.querySelector?.(`[data-profile-id="${runtime.savedProfileId}"]`);
      return modal?.getAttribute("aria-hidden") === "false"
        && selected?.classList?.contains("is-selected")
        && [...(modal?.querySelectorAll?.("[data-browser-action='load']") || [])]
          .some(button => button.textContent?.trim() === "Reload Saved Version");
    }
    if (predicateId === "saving.basic-restored") {
      return app.state.settings.loadedProfileRef?.id === basicId
        && Math.abs(Number(app.state.settings.config.t_max) - 3) < 1e-6
        && !app.commands.isSettingsProfileModified();
    }
    if (predicateId === "saving.settings-profile-deleted") {
      return !!runtime.savedProfileDeleted && !app.commands.findSettingsProfile(runtime.savedProfileId)
        && guidePredicateSatisfied("saving.basic-restored")
        && app.state.settings.userDefaultProfileId === runtime.names?.startupSettingsProfileId;
    }
    if (predicateId === "saving.solve-complete") {
      const run = app.state.solve.solveRuns.find(item => item.id === runtime.guideRunId);
      return !!run?.results
        && structurallyEqual(run.palette, primary?.filament_ids)
        && Math.abs(Number(run.config?.t_max) - 3) < 1e-6
        && run.image?.filename === runtime.tutorialImage?.filename
        && app.state.ui.currentTab === "solve"
        && !!app.state.ui.$(`#solveRunCards [data-run-id="${runtime.guideRunId}"]`)
        && !!app.state.ui.$(`[data-solve-card-kind][data-run-id="${runtime.guideRunId}"]`);
    }
    if (predicateId === "saving.run-saved") {
      const run = app.state.solve.solveRuns.find(item => item.id === runtime.guideRunId);
      return !!runtime.savedRunId
        && run?.saved_run_id === runtime.savedRunId
        && run?.label === runtime.names?.savedRunName;
    }
    if (predicateId === "saving.history-cleared") return runtime.historyCleared === true
      && app.state.solve.solveRuns.length === 0
      && !!runtime.savedRunId;
    if (predicateId === "saving.deck-cleared") return runtime.deckCleared === true && app.state.palette.deck.length === 0;
    if (predicateId === "saving.run-loaded") {
      const run = app.state.solve.solveRuns.find(item => item.id === runtime.loadedRunId);
      return !!run?.results
        && run.source_save_id === runtime.savedRunId
        && structurallyEqual(run.palette, primary?.filament_ids)
        && app.state.palette.activeDeckId != null
        && app.state.image.selectedImage?.filename === runtime.tutorialImage?.filename;
    }
    if (predicateId === "saving.run-settings-open") return runtime.runSettingsOpen === true;
    if (predicateId === "saving.temp-loaded") return app.state.settings.loadedProfileRef?.kind === "temporary"
      && app.state.settings.temporarySettingsProfile?.source?.run_id === runtime.loadedRunId;
    if (predicateId === "saving.run-save-deleted") {
      return runtime.savedRunDeleted === true
        && app.state.solve.solveRuns.some(run => run.id === runtime.loadedRunId)
        && app.state.settings.loadedProfileRef?.kind === "temporary";
    }
    return false;
  }

  function captureGuideCompletion(currentStep, detail = {}) {
    const runtime = context(app);
    if (currentStep.id === "save-palette" && detail.savedRecordId) runtime.savedPaletteId = detail.savedRecordId;
    if (currentStep.id === "load-saved-palette" && detail.deckCardId) runtime.loadedPaletteId = detail.deckCardId;
    if (currentStep.id === "delete-saved-palette-record" && detail.savedRecordId === runtime.savedPaletteId) runtime.savedPaletteDeleted = true;
    if (currentStep.id === "save-named-settings-profile" && detail.profileId) runtime.savedProfileId = detail.profileId;
    if (currentStep.id === "delete-named-settings-profile" && detail.profileId === runtime.savedProfileId) runtime.savedProfileDeleted = true;
    if (currentStep.id === "solve-for-saving-loading" && detail.runId) runtime.guideRunId = detail.runId;
    if (currentStep.id === "save-solved-run" && detail.runId === runtime.guideRunId) runtime.savedRunId = detail.saveId;
    if (currentStep.id === "clear-solve-history-for-load") runtime.historyCleared = true;
    if (currentStep.id === "clear-palette-deck-for-run-load") runtime.deckCleared = true;
    if (currentStep.id === "load-complete-saved-run" && detail.saveId === runtime.savedRunId) runtime.loadedRunId = detail.cardId;
    if (currentStep.id === "open-run-settings" && detail.runId === runtime.loadedRunId) runtime.runSettingsOpen = true;
    if (currentStep.id === "delete-saved-run-record" && detail.saveId === runtime.savedRunId) runtime.savedRunDeleted = true;
    if (
      currentStep.id === "printer-select"
      && !tutorialPrinterReady()
    ) {
      app.commands.showToast(
        "This guide requires the unmodified Tutorial Printer profile. End and restart the guide to restore it.",
        "warn",
      );
    }
    if (currentStep.id === "suggest-palettes") {
      const paletteColors = Number(app.state.ui.$("#targetFilamentCount")?.value);
      const suggestions = Number(app.state.ui.$("#targetSuggestCount")?.value);
      if (paletteColors !== 3 || suggestions < 2) {
        runtime.paletteA = null;
        runtime.paletteB = null;
        const correction = paletteColors !== 3 && suggestions < 2
          ? "Set Palette Colors to exactly 3 and Suggestions to at least 2, then try again."
          : paletteColors !== 3
            ? "Set Palette Colors to exactly 3, then try again."
            : "Set Suggestions to at least 2, then try again.";
        app.commands.showToast(correction, "warn");
        return;
      }
      const addedIds = Array.isArray(detail.cardIds) ? detail.cardIds : [];
      const cards = [...new Set(addedIds)]
        .map(id => app.state.palette.stagingDeck.find(card => card.id === id))
        .filter(card => Array.isArray(card?.filament_ids) && card.filament_ids.length === 3);
      if (cards.length >= 2) {
        runtime.paletteA = { id: cards[0].id, name: cards[0].name };
        runtime.paletteB = { id: cards[1].id, name: cards[1].name };
      } else {
        runtime.paletteA = null;
        runtime.paletteB = null;
        app.commands.showToast(
          "Prisma Generator Basics needs at least two new three-color suggestions. Keep Palette Colors at 3 and Suggestions at 2 or more, then try again.",
          "warn",
        );
      }
    }
    if (["manual-palette-add", "manual-palette-remove"].includes(currentStep.id)) {
      if (detail.action === "added" && detail.card?.id?.startsWith("manual-")) {
        runtime.manualCardId = detail.card.id;
        runtime.manualCardRemoved = false;
        app.commands.activateDeckCard(detail.card.id);
      } else if (detail.action === "removed" && detail.cardId === runtime.manualCardId) {
        runtime.manualCardRemoved = true;
      }
    }
    if (
      currentStep.id === "add-palette-variant"
      && detail.action === "added"
      && detail.card?.id?.startsWith("variant-")
      && detail.sourceCardId === runtime.paletteA?.id
    ) {
      runtime.variantCardId = detail.card.id;
      runtime.variantCardRemoved = false;
    }
    if (
      currentStep.id === "remove-palette-variant"
      && detail.action === "removed"
      && detail.cardId === runtime.variantCardId
    ) {
      runtime.variantCardRemoved = true;
    }
    if (currentStep.id === "advanced-settings" && detail.visible === true) {
      runtime.advancedSeenOn = true;
    }
    if (
      currentStep.id === "solve-first"
      && detail.runId
      && detail.deckCardId === runtime.paletteA?.id
      && tutorialPrinterReady({ requireNozzle: true })
    ) {
      runtime.runAId = detail.runId;
      app.state.solve.selectedRunIds = new Set([detail.runId]);
    }
    if (
      currentStep.id === "solve-second"
      && detail.runId
      && detail.deckCardId === runtime.paletteB?.id
      && detail.runId !== runtime.runAId
      && tutorialPrinterReady({ requireNozzle: true })
    ) {
      runtime.runBId = detail.runId;
      app.state.solve.selectedRunIds = new Set([runtime.runAId, detail.runId]);
    }
    if (currentStep.id === "generate-files" && detail.runId === runtime.runAId) {
      runtime.exportId = detail.exportId;
    }
  }

  function formatGuideText(value) {
    const runtime = context(app);
    const replacements = {
      "{{tutorialImage}}": runtime.tutorialImageFilename || "the tutorial image",
      "{{imagesFolder}}": runtime.imagesFolder || "the Images folder",
      "{{paletteA}}": runtime.paletteA?.name || "the first suggested palette",
      "{{paletteB}}": runtime.paletteB?.name || "the second suggested palette",
    };
    const resolvePath = path => path.split(".").reduce(
      (current, part) => (current == null ? undefined : current[part]),
      runtime,
    );
    for (const [token, path] of Object.entries(app.state.guides.currentGuide?.text_substitutions || {})) {
      const replacement = resolvePath(path);
      if (!["string", "number"].includes(typeof replacement)) {
        throw new Error(`Guide text substitution ${token} is unavailable`);
      }
      replacements[token] = String(replacement);
    }
    return Object.entries(replacements).reduce(
      (text, [token, replacement]) => text.split(token).join(replacement),
      String(value || ""),
    );
  }

  async function prepareGuideRuntime(guide) {
    return app.commands.prepareGuideWorkspace(guide);
  }

  async function cleanupGuideRuntime(guide, options = {}) {
    return app.commands.cleanupGuideWorkspace(guide, options);
  }

  function validateGuideTargetRegistry(guides = app.state.guides.definitions || []) {
    for (const guide of guides) {
      const guideSteps = [
        ...(guide?.steps || []),
        ...(guide?.detours || []).flatMap(current => current.steps || []),
      ];
      for (const currentStep of guideSteps) {
        if (
          currentStep.completion?.predicate_id
          && !KNOWN_GUIDE_PREDICATES.has(currentStep.completion.predicate_id)
        ) {
          throw new Error(`Unknown guide predicate: ${currentStep.completion.predicate_id}`);
        }
        if (currentStep.target_id === null) {
          if (currentStep.reveal_id && !KNOWN_GUIDE_REVEALS.has(currentStep.reveal_id)) {
            throw new Error(`Unknown guide reveal: ${currentStep.reveal_id}`);
          }
          continue;
        }
        const descriptor = TARGETS[currentStep.target_id];
        if (!descriptor) throw new Error(`Unknown guide target: ${currentStep.target_id}`);
        if (descriptor.reveal_id !== currentStep.reveal_id) {
          throw new Error(`Guide target ${currentStep.target_id} has a mismatched reveal id`);
        }
      }
      for (const currentDetour of guide.detours || []) {
        if (
          currentDetour.return_predicate_id
          && !KNOWN_GUIDE_PREDICATES.has(currentDetour.return_predicate_id)
        ) {
          throw new Error(`Unknown guide predicate: ${currentDetour.return_predicate_id}`);
        }
      }
    }
    return true;
  }

  Object.assign(app.commands, {
    captureGuideCompletion,
    captureGuidePresentation,
    cleanupGuideRuntime,
    formatGuideText,
    guidePredicateSatisfied,
    prepareGuideRuntime,
    resolveGuideTarget,
    resolveGuideTargetLayout,
    resolveGuideTargetRegions,
    restoreGuidePresentation,
    revealGuideTarget,
    validateGuideTargetRegistry,
  });
  app.state.guides.targets = TARGETS;
}
