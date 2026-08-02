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

function context(app) {
  return app.state.guides.runtimeContext || {};
}

function cardById(root, cardId, attribute = "data-card-id") {
  if (!root || !cardId) return null;
  return [...root.querySelectorAll(`[${attribute}]`)]
    .find(element => element.getAttribute(attribute) === cardId) || null;
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
  "sidebar.printer": staticTarget('[data-guide-target="sidebar.printer"]', "sidebar.printer"),
  "sidebar.active-printer": staticTarget('[data-guide-target="sidebar.active-printer"]', "sidebar.active-printer"),
  "printer.configuration": staticTarget('[data-guide-target="printer.configuration"]', "printer.configuration"),
  "sidebar.active-nozzle": staticTarget('[data-guide-target="sidebar.active-nozzle"]', "sidebar.active-nozzle"),
  "sidebar.model-library": staticTarget('[data-guide-target="sidebar.model-library"]', "sidebar.model-library"),
  "sidebar.active-filaments": staticTarget('[data-guide-target="sidebar.active-filaments"]', "sidebar.active-filaments"),
  "topbar.theme": staticTarget('[data-guide-target="topbar.theme"]', "topbar.theme"),
  "topbar.clear-temp": staticTarget('[data-guide-target="topbar.clear-temp"]', "topbar.clear-temp"),
  "topbar.help-guides": staticTarget('[data-guide-target="topbar.help-guides"]', "topbar.help-guides"),
  "topbar.settings": staticTarget('[data-guide-target="topbar.settings"]', "topbar.settings"),
  "topbar.solve": staticTarget("#startSolveBtn", "topbar.solve"),
  "workflow.tabs": staticTarget('[data-guide-target="workflow.tabs"]', "workflow.tabs"),
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
  "image.library-management": groupedTarget([
    { selector: "#imageLibraryPanel", all: false },
    { selector: ".library-title-actions", all: false },
  ], "image.library-management"),
  "image.preview": staticTarget("#imagePreviewPane", "image.preview"),
  "image.adjustments": staticTarget(".framing-editor", "image.adjustments"),
  "image.aspect-experiment": groupedTarget([
    { selector: "#frameCanvasWrap, #directionToggle, #arButtonGroup", all: true },
  ], "image.aspect-experiment"),
  "image.aspect-controls": staticTarget("#frameControlsSize", "image.aspect-controls"),
  "image.framing": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    {
      selector: '[data-guide-target-part="image.transform-controls"]',
      all: true,
    },
  ], "image.framing"),
  "image.adjustment-image-tab": staticTarget(
    '[data-guide-target="image.adjustment-image-tab"]',
    "image.adjustment-image-tab",
  ),
  "image.appearance": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    { selector: "#frameControlsImage", all: false },
  ], "image.appearance"),
  "image.border": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    { selector: '[data-guide-target-part="image.border-controls"]', all: true },
  ], "image.border"),
  "image.reset-framing": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    { selector: '[data-guide-target-part="image.canvas-reset"]', all: true },
  ], "image.reset-framing"),
  "image.crop-fit": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    { selector: "#fitImageBtn, #fillWidthBtn, #fillHeightBtn", all: true },
  ], "image.crop-fit"),
  "image.physical-dimensions": groupedTarget([
    { selector: '[data-guide-target-part="image.canvas-dimensions"]', all: true },
  ], "image.physical-dimensions"),
  "image.tutorial-canvas": groupedTarget([
    { selector: "#frameCanvasWrap", all: false },
    { selector: "#arButtonGroup", all: false },
    { selector: '[data-guide-target-part="image.canvas-dimensions"]', all: true },
  ], "image.tutorial-canvas"),
  "image.canvas-settings": staticTarget("#frameControlsSize", "image.canvas-settings"),
  "image.summary": staticTarget("#imageInfoGrid", "image.summary"),
  "basics.tutorial-image": dynamicTarget((app) => {
    const filename = context(app).tutorialImageFilename;
    return [...(app.state.ui.$("#imageGrid")?.querySelectorAll(".image-card") || [])]
      .find(card => card.dataset.filename === filename) || null;
  }, "basics.tutorial-image"),
  "palette.modes": staticTarget(".creation-mode-tabs", "palette.modes"),
  "palette.autosuggest-overview": groupedTarget([
    { selector: ".creation-mode-tabs", all: false },
    { selector: "#candidateGrid", all: false },
  ], "palette.autosuggest-overview"),
  "palette.candidates": staticTarget("#candidateGrid", "palette.candidates"),
  "palette.suggest": staticTarget("#suggestPalettesBtn", "palette.suggest"),
  "palette.suggestions": staticTarget("#deckCards", "palette.suggestions"),
  "palette.deck": staticTarget("#railDeckList", "palette.deck"),
  "palette.manual": groupedTarget([
    { selector: "#panelManualBuilder", all: false },
    { selector: "#manualPalettePanel", all: false },
  ], "palette.manual"),
  "basics.manual-card": dynamicTarget(
    app => cardById(app.state.ui.$("#railDeckList"), context(app).manualCardId),
    "basics.manual-card",
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
});

const KNOWN_GUIDE_PREDICATES = new Set([
  "settings.drawer-open",
  "settings.drawer-closed",
  "printer-config.open",
  "printer-config.closed",
  "basics.tutorial-printer-active",
  "basics.tutorial-nozzle-active",
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

/** Install stable guide-target resolution, runtime capture, and presentation-only reveals. */
export function installFeaturesGuidesTargets(app) {
  function resolveGuideTargetRegions(targetId) {
    const descriptor = TARGETS[targetId];
    if (!descriptor) return null;
    if (descriptor.regions) {
      const regions = descriptor.regions.map((region) => {
        const elements = region.all
          ? [...app.state.ui.$$(region.selector)]
          : [app.state.ui.$(region.selector)];
        return elements.filter(Boolean);
      });
      return regions.every(region => region.length > 0) ? regions : null;
    }
    const target = descriptor.resolve
      ? descriptor.resolve(app)
      : app.state.ui.$(descriptor.selector);
    return target ? [[target]] : null;
  }

  function resolveGuideTarget(targetId) {
    return resolveGuideTargetRegions(targetId)?.[0]?.[0] || null;
  }

  function captureGuidePresentation() {
    return {
      currentTab: app.state.ui.currentTab || "image",
      settingsDrawerOpen: !!app.state.settings.settingsDrawerOpen,
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
    if (revealId === "printer.configuration") {
      if (reviewOnly) return;
      const page = app.state.ui.$("#printerConfigPage");
      if (page?.classList.contains("is-hidden")) app.commands.showPrinterConfigPage();
      return;
    }
    if (revealId === "topbar.settings") {
      if (!reviewOnly && app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
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
    ) {
      if (["palette.deck", "basics.palette-a", "basics.palette-b", "basics.manual-card"].includes(revealId)) {
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
      && frame.arMode === "image"
      && Math.abs(Number(frame.widthMm) - 120) < 0.001
      && Math.abs(Number(frame.heightMm) - 160) < 0.001
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
      return app.state.image.selectedImage?.filename === runtime.tutorialImageFilename;
    }
    if (predicateId === "basics.image-reset") {
      return tutorialImageIsReset();
    }
    if (predicateId === "basics.canvas-ready") {
      const frame = app.state.image.frameState || {};
      return app.state.image.selectedImage?.filename === runtime.tutorialImageFilename
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
      return !!runtime.paletteA?.id && !!runtime.paletteB?.id;
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
    if (predicateId === "basics.palette-a-active") {
      return guidePredicateSatisfied("basics.tutorial-palettes-in-deck")
        && app.state.palette.activeDeckId === runtime.paletteA?.id;
    }
    if (predicateId === "basics.palette-b-active") {
      return guidePredicateSatisfied("basics.tutorial-palettes-in-deck")
        && app.state.palette.activeDeckId === runtime.paletteB?.id;
    }
    if (predicateId === "basics.tutorial-profile-ready") {
      return app.state.settings.loadedProfileRef?.id === "temporary-guide-basics"
        && !app.commands.isSettingsProfileModified();
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
    return false;
  }

  function captureGuideCompletion(currentStep, detail = {}) {
    const runtime = context(app);
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
      const addedIds = Array.isArray(detail.cardIds) ? detail.cardIds : [];
      const cards = addedIds
        .map(id => app.state.palette.stagingDeck.find(card => card.id === id))
        .filter(Boolean);
      if (cards.length >= 2) {
        runtime.paletteA = { id: cards[0].id, name: cards[0].name };
        runtime.paletteB = { id: cards[1].id, name: cards[1].name };
      } else {
        runtime.paletteA = null;
        runtime.paletteB = null;
        app.commands.showToast(
          "Prisma Generator Basics needs at least two new suggestions. Keep the tutorial filament selection and suggestion controls unchanged, then try again.",
          "warn",
        );
      }
    }
    if (["manual-palette-add", "manual-palette-remove"].includes(currentStep.id)) {
      if (detail.action === "added" && detail.card?.id?.startsWith("manual-")) {
        runtime.manualCardId = detail.card.id;
        runtime.manualCardRemoved = false;
      } else if (detail.action === "removed" && detail.cardId === runtime.manualCardId) {
        runtime.manualCardRemoved = true;
      }
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
      "{{paletteA}}": runtime.paletteA?.name || "the first suggested palette",
      "{{paletteB}}": runtime.paletteB?.name || "the second suggested palette",
    };
    return Object.entries(replacements).reduce(
      (text, [token, replacement]) => text.split(token).join(replacement),
      String(value || ""),
    );
  }

  async function prepareGuideRuntime(guide) {
    app.state.guides.runtimeContext = {};
    if (guide.prepare_id !== "basics") return true;
    const requirements = {
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: true,
      ...(guide.preparation || {}),
    };
    if (
      app.state.image.activeImportBatchId
      || app.state.ui.activeSuggestJobId
      || app.state.solve.solveStartPending
      || app.state.solve.paletteBatchStartPending
      || app.state.solve.activeSolveJobId
      || app.state.export.exportRunning
      || app.state.export.activeExportJobId
    ) {
      app.commands.showToast(`Finish the current operation before starting ${guide.title}.`, "warn");
      return false;
    }
    if (app.state.palette.manualVariantDraft) {
      app.commands.showToast(
        `Finish or cancel the current Manual palette variant before starting ${guide.title}.`,
        "warn",
      );
      return false;
    }
    const tutorialControls = ["targetFilamentCount", "targetSwapCount", "targetSuggestCount"];
    const initialControlValues = requirements.palette_controls
      ? Object.fromEntries(
        tutorialControls.map(id => [id, app.state.ui.$(`#${id}`)?.value ?? null]),
      )
      : null;
    const initialCandidateSelection = requirements.palette_controls
      ? [...app.state.palette.candidateSelection]
      : null;
    const initialCandidateInitialized = requirements.palette_controls
      ? app.state.palette.candidateInitialized
      : null;
    const initialSettingsState = requirements.tutorial_settings
      ? app.commands._captureLiveSettingsProfileState()
      : null;
    let prepared;
    try {
      const prepareOptions = {
        includeTutorialPrinter: requirements.tutorial_printer,
        includeTutorialImage: requirements.tutorial_image,
      };
      prepared = await app.api.prepareBasicsGuide(prepareOptions);
      if (requirements.tutorial_printer && prepared.tutorial_printer?.status !== "ready") {
        const condition = prepared.tutorial_printer?.status === "modified" ? "modified" : "missing";
        const restore = await app.commands.appConfirm(
          `The Tutorial Printer profile is ${condition}. Restore the built-in tutorial copy? Other printer profiles will not be changed.`,
          { title: "Restore Tutorial Printer", ok: "Restore", cancel: "Cancel Guide" },
        );
        if (!restore) return false;
        prepared = await app.api.prepareBasicsGuide({
          ...prepareOptions,
          restoreTutorialPrinter: true,
        });
      }
      if (
        (requirements.tutorial_printer && (
          prepared.tutorial_printer?.status !== "ready"
          || !prepared.tutorial_printer?.profile
        ))
        || (requirements.tutorial_image && !prepared.tutorial_image?.filename)
      ) {
        throw new Error("the tutorial inputs were not returned in a usable state");
      }
      if (requirements.tutorial_printer) await app.commands.loadPrinters();
      if (requirements.tutorial_image) app.commands.upsertImageLibraryEntry(prepared.tutorial_image);
      if (requirements.tutorial_settings) {
        const systemProfile = app.commands.findSettingsProfile(app.state.ui.SYSTEM_SETTINGS_PROFILE_ID);
        if (!systemProfile) throw new Error("the built-in Basic Settings Profile is unavailable");
        const now = new Date().toISOString();
        const tutorialProfile = {
          ...app.commands._cloneValue(systemProfile),
          id: "temporary-guide-basics",
          kind: "temporary",
          name: "Tutorial Basics",
          settings: {
            ...app.commands._cloneValue(systemProfile.settings || {}),
            image_sample_pitch_mm: 0.4,
            solver_fine_pitch_mm: 0.4,
          },
          modules: app.commands._cloneValue(systemProfile.modules || {}),
          source: {
            kind: "guide",
            label: guide.title,
          },
          created_at: now,
          updated_at: now,
        };
        await app.commands._doLoadSettingsProfile(tutorialProfile);
      }
    } catch (error) {
      app.commands.showToast(`Could not prepare ${guide.title}: ${error.message}`, "error");
      return false;
    }
    app.state.guides.runtimeContext = {
      tutorialImageFilename: prepared.tutorial_image?.filename || null,
      tutorialPrinterProfile: prepared.tutorial_printer?.profile || null,
      paletteA: null,
      paletteB: null,
      manualCardId: null,
      manualCardRemoved: false,
      advancedSeenOn: false,
      runAId: null,
      runBId: null,
      exportId: null,
      initialCandidateSelection,
      initialCandidateInitialized,
      initialControlValues,
      initialSettingsState,
      preparation: requirements,
    };
    if (requirements.palette_controls) {
      app.commands.selectAllCandidates();
      app.state.palette.candidateInitialized = true;
      const tutorialValues = {
        targetFilamentCount: "3",
        targetSwapCount: "0",
        targetSuggestCount: "5",
      };
      for (const [id, value] of Object.entries(tutorialValues)) {
        const input = app.state.ui.$(`#${id}`);
        if (input) input.value = value;
      }
    }
    return true;
  }

  async function cleanupGuideRuntime(guide) {
    const runtime = context(app);
    if (guide?.prepare_id !== "basics") return;
    if (runtime.initialControlValues) {
      app.state.palette.candidateSelection = new Set(runtime.initialCandidateSelection || []);
      app.state.palette.candidateInitialized = !!runtime.initialCandidateInitialized;
      for (const [id, value] of Object.entries(runtime.initialControlValues)) {
        const input = app.state.ui.$(`#${id}`);
        if (input && value !== null) input.value = value;
      }
    }
    if (runtime.initialSettingsState) {
      await app.commands._restoreLiveSettingsProfileState(runtime.initialSettingsState);
    }
    if (app.state.ui.currentTab === "creation") app.commands.renderCreationTab?.();
  }

  function validateGuideTargetRegistry() {
    for (const guide of app.state.guides.definitions || []) {
      for (const currentStep of app.commands.getAllGuideSteps(guide)) {
        if (
          currentStep.completion?.predicate_id
          && !KNOWN_GUIDE_PREDICATES.has(currentStep.completion.predicate_id)
        ) {
          throw new Error(`Unknown guide predicate: ${currentStep.completion.predicate_id}`);
        }
        if (currentStep.target_id === null && currentStep.reveal_id === null) continue;
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
    resolveGuideTargetRegions,
    restoreGuidePresentation,
    revealGuideTarget,
    validateGuideTargetRegistry,
  });
  app.state.guides.targets = TARGETS;
}
