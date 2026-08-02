"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const {
  appDir,
  createFeatureHarness,
  fakeElement,
} = require("./support/application_harness.cjs");

const moduleUrl = relative => pathToFileURL(path.join(appDir, relative)).href;

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function tutorialPrinterProfile() {
  return {
    id: "tutorial-printer",
    name: "Tutorial Printer",
    max_print_area: { x: 256, y: 256 },
    nozzle_profiles: [
      { size: 0.2 },
      { size: 0.4 },
    ],
  };
}

function setTutorialPrinterState(app, profile = tutorialPrinterProfile(), nozzleSize = 0.4) {
  app.state.session.printersData = {
    printers: [clone(profile)],
    active_printer_id: "tutorial-printer",
    active_nozzle_size: nozzleSize,
  };
  return profile;
}

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      const registered = listeners.get(type) || new Set();
      registered.add(listener);
      listeners.set(type, registered);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    dispatch(type, event = {}) {
      for (const listener of [...(listeners.get(type) || [])]) listener(event);
    },
  };
}

function menuElement(documentTarget, attributes = {}) {
  const target = eventTarget();
  const values = new Map(Object.entries(attributes));
  return Object.assign(target, {
    dataset: {},
    disabled: false,
    hidden: false,
    items: [],
    offsetHeight: 100,
    offsetWidth: 220,
    style: {},
    contains(node) { return node === this || this.items.includes(node); },
    closest() { return this; },
    focus() { documentTarget.activeElement = this; },
    getAttribute(name) { return values.get(name) ?? null; },
    getBoundingClientRect() {
      return { left: 700, right: 800, top: 10, bottom: 40, width: 100, height: 30 };
    },
    querySelectorAll() { return this.items; },
    setAttribute(name, value) { values.set(name, String(value)); },
  });
}

test("semantic event bus emits safely and disposes subscriptions", async () => {
  const { createEventBus } = await import(moduleUrl("core/events.js"));
  const events = createEventBus();
  const received = [];
  const dispose = events.subscribe("settings.opened", detail => received.push(detail));

  assert.equal(events.emit("settings.opened", { source: "drawer" }), 1);
  dispose();
  assert.equal(events.emit("settings.opened", { source: "other" }), 0);
  assert.deepEqual(received, [{ source: "drawer" }]);

  const reported = [];
  const originalError = console.error;
  console.error = (...args) => reported.push(args);
  try {
    events.subscribe("solve.completed", () => {
      throw new Error("guide listener failed");
    });
    events.subscribe("solve.completed", detail => received.push(detail));
    assert.equal(events.emit("solve.completed", { runId: "run-1" }), 2);
  } finally {
    console.error = originalError;
  }
  assert.deepEqual(received.at(-1), { runId: "run-1" });
  assert.match(reported[0][0], /listener for "solve\.completed" failed/);
});

test("guide emphasis renders strong text without interpreting arbitrary markup", async () => {
  const { renderGuideTextContent } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const documentTarget = {
    createElement(tagName) {
      return { tagName: tagName.toUpperCase(), textContent: "" };
    },
    createTextNode(text) {
      return { nodeType: 3, textContent: text };
    },
  };
  const element = {
    ownerDocument: documentTarget,
    children: [],
    replaceChildren(...children) { this.children = children; },
  };

  renderGuideTextContent(element, "A **palette** and a **solve** <remain text>.");

  assert.deepEqual(
    element.children.map(child => [child.tagName || "TEXT", child.textContent]),
    [
      ["TEXT", "A "],
      ["STRONG", "palette"],
      ["TEXT", " and a "],
      ["STRONG", "solve"],
      ["TEXT", " <remain text>."],
    ],
  );
});

test("guide geometry selects adjacent placements and deterministic docks", async () => {
  const {
    alignRectToCssPixels,
    chooseGuideCardPlacement,
    intersectRects,
    shadeRects,
    shadeRectsAroundTargets,
  } = await import(moduleUrl("core/guide-geometry.js"));
  const viewportRect = { left: 0, top: 0, right: 1000, bottom: 700, width: 1000, height: 700 };
  const targetRect = { left: 400, top: 250, right: 600, bottom: 350, width: 200, height: 100 };

  assert.equal(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 260, height: 140 },
    viewportRect,
    preferred: ["bottom"],
  }).placement, "bottom");
  for (const placement of ["top", "right", "left"]) {
    assert.equal(chooseGuideCardPlacement({
      targetRect,
      cardSize: { width: 260, height: 140 },
      viewportRect,
      preferred: [placement],
    }).placement, placement);
  }
  assert.equal(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 980, height: 680 },
    viewportRect,
  }).placement, "dock-bottom-right");
  assert.equal(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 260, height: 140 },
    viewportRect,
    dockIndex: 2,
  }).placement, "dock-top-right");
  assert.deepEqual(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 260, height: 140 },
    viewportRect,
    previousPosition: { left: 40, top: 40 },
  }), {
    left: 40,
    top: 40,
    placement: "retained",
    docked: false,
  });
  assert.equal(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 260, height: 140 },
    viewportRect,
    preferred: ["bottom"],
    previousPosition: { left: 420, top: 260 },
  }).placement, "bottom");
  assert.deepEqual(chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 260, height: 140 },
    viewportRect,
    previousPosition: { left: 990, top: 690 },
  }), {
    left: 732,
    top: 552,
    placement: "retained",
    docked: false,
  });
  assert.deepEqual(
    intersectRects(
      { left: -10, top: 10, right: 40, bottom: 60 },
      { left: 0, top: 0, right: 30, bottom: 30 },
    ),
    { left: 0, top: 10, right: 30, bottom: 30, width: 30, height: 20 },
  );
  assert.equal(shadeRects(targetRect, viewportRect).length, 4);
  const upperScrim = alignRectToCssPixels({
    left: 0.2,
    top: 100.4,
    right: 700.3,
    bottom: 250.6,
  });
  const lowerScrim = alignRectToCssPixels({
    left: 0.2,
    top: 250.6,
    right: 700.3,
    bottom: 350.2,
  });
  assert.equal(upperScrim.bottom, lowerScrim.top);
  assert.equal(upperScrim.top + upperScrim.height, lowerScrim.top);
  const spotlightTargets = [
    { left: 700, top: 100, right: 950, bottom: 250 },
    { left: 700, top: 350, right: 950, bottom: 500 },
  ];
  const multiTargetShade = shadeRectsAroundTargets(spotlightTargets, viewportRect);
  const shadedArea = multiTargetShade.reduce(
    (total, rect) => total + (rect.width * rect.height),
    0,
  );
  assert.equal(shadedArea, (1000 * 700) - (2 * 250 * 150));
  assert.ok(multiTargetShade.every(rect => (
    spotlightTargets.every(target => !intersectRects(rect, target))
  )));
  const oversized = chooseGuideCardPlacement({
    targetRect,
    cardSize: { width: 4000, height: 4000 },
    viewportRect,
  });
  assert.ok(oversized.left >= viewportRect.left);
  assert.ok(oversized.top >= viewportRect.top);
  assert.ok(oversized.left < viewportRect.right);
  assert.ok(oversized.top < viewportRect.bottom);
});

test("anchored guide menu supports keyboard navigation and focus restoration", async () => {
  const { createAnchoredMenuController } = await import(moduleUrl("core/anchored-menu.js"));
  const documentTarget = Object.assign(eventTarget(), {
    activeElement: null,
    documentElement: { clientWidth: 1000, clientHeight: 700 },
  });
  const viewportTarget = Object.assign(eventTarget(), {
    innerWidth: 1000,
    innerHeight: 700,
    visualViewport: null,
  });
  const button = menuElement(documentTarget);
  const menu = menuElement(documentTarget);
  menu.hidden = true;
  const first = menuElement(documentTarget, { role: "menuitem" });
  const second = menuElement(documentTarget, { role: "menuitem" });
  const third = menuElement(documentTarget, { role: "menuitem" });
  menu.items = [first, second, third];
  const activated = [];
  const controller = createAnchoredMenuController({
    button,
    menu,
    documentTarget,
    viewportTarget,
    onActivate: item => activated.push(item),
  });
  const preventDefault = () => {};

  controller.open();
  assert.equal(documentTarget.activeElement, first);
  menu.dispatch("keydown", { key: "End", target: first, preventDefault });
  assert.equal(documentTarget.activeElement, third);
  menu.dispatch("keydown", { key: "Home", target: third, preventDefault });
  assert.equal(documentTarget.activeElement, first);
  menu.dispatch("keydown", { key: "Escape", target: first, preventDefault });
  assert.equal(menu.hidden, true);
  assert.equal(documentTarget.activeElement, button);

  controller.open();
  menu.dispatch("click", { target: second });
  assert.deepEqual(activated, [second]);
  assert.equal(menu.hidden, true);
  assert.equal(documentTarget.activeElement, button);

  controller.open();
  menu.dispatch("keydown", { key: "Tab", target: first, preventDefault });
  assert.equal(menu.hidden, true);
  assert.equal(documentTarget.activeElement, first);
  controller.destroy();
});

test("guide definitions and target registry are stable and complete", async () => {
  const { app } = await createFeatureHarness();

  assert.equal(app.commands.validateGuideDefinitions(), true);
  assert.equal(app.commands.validateGuideTargetRegistry(), true);
  const guide = app.commands.getGuideDefinition("interface-preview");
  assert.deepEqual(
    guide.steps.map(step => step.id),
    ["workflow-tabs", "settings-button", "white-point-rescale", "image-library"],
  );
  assert.equal(guide.steps[1].completion.event, "settings.opened");
  assert.equal(guide.steps[1].completion.predicate_id, "settings.drawer-open");
  const guidedSetup = app.commands.getGuideDefinition("guided-setup");
  assert.deepEqual(
    guidedSetup.steps.map(step => step.id),
    [
      "printer-open",
      "printer-configuration",
      "active-nozzle",
      "model-library",
      "active-filaments",
      "theme",
      "temporary-files",
      "help-and-guides",
      "complete",
    ],
  );
  assert.equal(guidedSetup.restore_presentation, false);
  assert.equal(guidedSetup.steps[0].completion.event, "printer-config.opened");
  assert.equal(guidedSetup.steps[0].completion.accept_preexisting, true);
  assert.equal(guidedSetup.steps[1].completion.event, "printer-config.closed");
  assert.equal(guidedSetup.steps[1].allow_previous, false);
  assert.equal(guidedSetup.steps.at(-1).target_id, null);
  const modelLibraryStep = guidedSetup.steps.find(step => step.id === "model-library");
  assert.match(modelLibraryStep.body, /change the active Model Library/i);
  assert.match(modelLibraryStep.body, /leave it unchanged during this guide/i);
  assert.doesNotMatch(modelLibraryStep.body, /install and select/i);
  assert.equal(
    app.commands.getGuideDefinition("guided-setup-help-pointer").steps[0].completion.kind,
    "interaction",
  );
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  assert.equal(basics.prepare_id, "basics");
  assert.equal(basics.version, 7);
  assert.equal(basics.steps.length, 34);
  assert.ok(basics.steps.length <= 35);
  assert.ok(basics.steps.findIndex(step => step.id === "solve-first") <= 21);
  assert.deepEqual(
    basics.chapters.map(current => current.label),
    ["Introduction", "Image", "Palette", "Settings", "Preview", "Export"],
  );
  assert.deepEqual(
    basics.detours.map(current => current.id),
    ["image-controls", "palette-tools", "settings-tools", "preview-tools", "export-choices"],
  );
  assert.equal(basics.steps[0].id, "introduction");
  assert.equal(basics.steps[0].target_id, null);
  assert.equal(basics.steps[0].next_label, "Begin");
  assert.equal(basics.steps[0].allow_previous, false);
  assert.equal(basics.steps[1].id, "core-terminology");
  assert.equal(basics.steps[1].target_id, null);
  assert.match(basics.steps[1].body, /• A \*\*palette\*\*/);
  assert.match(basics.steps[1].body, /• A \*\*solve\*\*/);
  assert.equal(basics.steps[2].id, "workflow");
  assert.equal(basics.steps[2].target_id, "workflow.overview");
  assert.match(
    basics.steps[2].body,
    /Image → Palette → Preview → Export\n\nSettings[\s\S]*\n\nEach stage/,
  );
  assert.equal(basics.steps[3].id, "printer-select");
  assert.equal(basics.steps[3].target_id, "sidebar.active-printer");
  assert.equal(basics.steps[3].completion.event, "printer.active-changed");
  assert.equal(basics.steps[3].completion.accept_preexisting, true);
  assert.equal(basics.steps[4].id, "tutorial-nozzle");
  assert.equal(basics.steps[3].placement_group, "basics-printer");
  assert.equal(basics.steps[4].placement_group, "basics-printer");
  for (const stepId of [
    "image-introduction",
    "palette-introduction",
    "preview-introduction",
    "export-introduction",
  ]) {
    const chapterStep = basics.steps.find(step => step.id === stepId);
    assert.equal(chapterStep.target_id, null);
    assert.equal(chapterStep.completion.kind, "manual");
    assert.equal(chapterStep.placement_group, undefined);
  }
  assert.equal(
    app.commands.getGuideStep(basics, "aspect-ratios").placement_group,
    "basics-image-adjustments",
  );
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-canvas").placement_group,
    "basics-image-adjustments",
  );
  assert.equal(
    basics.steps.find(step => step.id === "settings-profile").placement_group,
    "basics-settings",
  );
  assert.equal(
    basics.steps.findIndex(step => step.id === "image-introduction") + 1,
    basics.steps.findIndex(step => step.id === "choose-image"),
  );
  assert.equal(
    basics.steps.findIndex(step => step.id === "palette-introduction") + 1,
    basics.steps.findIndex(step => step.id === "palette-methods"),
  );
  const paletteIntroduction = app.commands.getGuideStep(basics, "palette-introduction");
  assert.equal(paletteIntroduction.title, "Palette: choose your filaments");
  assert.match(
    paletteIntroduction.body,
    /A \*\*palette\*\* is the set of filaments Prisma will use to recreate the colors in an image/,
  );
  assert.doesNotMatch(paletteIntroduction.body, /\bordered\b|above the white base/i);
  const paletteMethods = app.commands.getGuideStep(basics, "palette-methods");
  assert.match(paletteMethods.body, /Auto-Suggest recommends palettes/);
  assert.match(paletteMethods.body, /only the filaments selected in this panel/);
  assert.match(paletteMethods.body, /Deselecting a filament prevents Auto-Suggest/);
  assert.match(paletteMethods.body, /base and White Cap in Settings/);
  assert.match(paletteMethods.body, /cannot be selected as a palette color/);
  assert.equal(
    basics.steps.findIndex(step => step.id === "solve-first") + 1,
    basics.steps.findIndex(step => step.id === "preview-introduction"),
  );
  assert.equal(
    basics.steps.findIndex(step => step.id === "export-introduction") + 1,
    basics.steps.findIndex(step => step.id === "select-export-run"),
  );
  assert.equal(
    app.commands.getGuideStep(basics, "aspect-ratios").target_id,
    "image.aspect-experiment",
  );
  assert.equal(
    app.commands.getGuideStep(basics, "interactive-framing").target_id,
    "image.framing",
  );
  const imagePreviewStep = app.commands.getGuideStep(basics, "image-preview");
  assert.equal(imagePreviewStep.target_id, "image.adjustments");
  assert.equal(imagePreviewStep.placement_group, "basics-image-adjustments");
  assert.equal(imagePreviewStep.preferred_placements[0], "left");
  const imageDetour = app.commands.getGuideDetour(basics, "image-controls");
  const framingIndex = imageDetour.steps.findIndex(step => step.id === "interactive-framing");
  assert.deepEqual(
    imageDetour.steps.slice(framingIndex + 1).map(step => step.id),
    [
      "physical-dimensions",
      "border",
      "image-summary",
      "open-image-adjustments",
      "appearance",
    ],
  );
  const openImageAdjustments = imageDetour.steps.find(
    step => step.id === "open-image-adjustments",
  );
  assert.equal(openImageAdjustments.target_id, "image.adjustment-image-tab");
  assert.equal(openImageAdjustments.completion.kind, "event");
  assert.equal(
    openImageAdjustments.completion.event,
    "image.adjustment-tab.changed",
  );
  assert.equal(
    openImageAdjustments.completion.predicate_id,
    "image.adjustment-tab-image",
  );
  assert.equal(openImageAdjustments.completion.accept_preexisting, true);
  assert.equal(openImageAdjustments.allow_skip, false);
  assert.equal(app.commands.getGuideStep(basics, "crop-fit").target_id, "image.crop-fit");
  assert.equal(
    app.commands.getGuideStep(basics, "physical-dimensions").target_id,
    "image.physical-dimensions",
  );
  assert.equal(app.commands.getGuideStep(basics, "border").target_id, "image.border");
  const imageSummary = app.commands.getGuideStep(basics, "image-summary");
  assert.match(imageSummary.body, /Solve Pitch \(the physical spacing between pixels/);
  assert.doesNotMatch(imageSummary.body, /usually requires more time/);
  const tutorialCanvas = basics.steps.find(step => step.id === "tutorial-canvas");
  assert.match(tutorialCanvas.body, /same canvas size keeps the tutorial results consistent/);
  assert.match(tutorialCanvas.body, /either W×H or Image/);
  assert.match(tutorialCanvas.body, /canvas is 90 × 120 mm and the border is off/);
  for (const awkwardConclusion of [
    /no particular choice is required yet/,
    /after this optional section/,
    /The next required step/,
    /These persistence tools are optional here/,
  ]) {
    assert.ok(
      app.commands.getAllGuideSteps(basics).every(step => !awkwardConclusion.test(step.body)),
    );
  }
  assert.equal(
    app.commands.getGuideStep(basics, "image-library").target_id,
    "image.library-management",
  );
  assert.doesNotMatch(
    app.commands.getGuideStep(basics, "image-library").body,
    /prepares imported images|modifying their originals/i,
  );
  const resetImageControls = basics.steps.find(step => step.id === "reset-image-controls");
  assert.equal(resetImageControls.target_id, "image.reset-framing");
  assert.equal(resetImageControls.completion.predicate_id, "basics.image-reset");
  assert.equal(basics.steps.find(step => step.id === "tutorial-canvas").target_id, "image.tutorial-canvas");
  assert.equal(app.commands.getGuideStep(basics, "manual-palette-add").target_id, "palette.manual");
  assert.equal(app.commands.getGuideStep(basics, "manual-palette-remove").target_id, "basics.manual-card");
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-nozzle").completion.kind,
    "manual",
  );
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-nozzle").completion.predicate_id,
    "basics.tutorial-nozzle-active",
  );
  assert.match(
    basics.steps.find(step => step.id === "tutorial-nozzle").body,
    /0\.4 mm/,
  );
  const terminologyStep = basics.steps.find(step => step.id === "core-terminology");
  assert.match(terminologyStep.body, /A \*\*palette\*\* is/);
  assert.match(terminologyStep.body, /A \*\*solve\*\* is/);
  assert.doesNotMatch(terminologyStep.body, /[“"](?:palette|solve)[”"]/i);
  const filamentSelectionStep = app.commands.getGuideStep(basics, "candidate-filaments");
  assert.doesNotMatch(`${filamentSelectionStep.title}\n${filamentSelectionStep.body}`, /\bcandidate/i);
  for (const definition of [guidedSetup, basics, app.commands.getGuideDefinition("guided-setup-help-pointer")]) {
    for (const currentStep of definition.steps) {
      assert.doesNotMatch(`${currentStep.title}\n${currentStep.body}`, /\bwalkthrough\b/i);
    }
  }
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-canvas").completion.predicate_id,
    "basics.canvas-ready",
  );
  const openPalette = basics.steps.find(step => step.id === "open-palette");
  assert.equal(openPalette.completion.kind, "event");
  assert.equal(openPalette.completion.event, "tab.changed");
  assert.equal(openPalette.completion.predicate_id, "tab.creation");
  assert.equal(openPalette.completion.accept_preexisting, false);
  assert.equal(openPalette.allow_skip, false);
  assert.equal(
    basics.steps.find(step => step.id === "suggest-palettes").completion.event,
    "palette.suggestions.completed",
  );
  assert.equal(
    basics.steps.find(step => step.id === "solve-first").completion.event,
    "solve.completed",
  );
  assert.equal(
    basics.steps.find(step => step.id === "generate-files").completion.event,
    "export.completed",
  );
  const imageGuide = app.commands.getGuideDefinition("image-guide");
  const settingsGuide = app.commands.getGuideDefinition("settings-guide");
  assert.equal(
    app.commands.getGuideStep(imageGuide, "aspect-ratios"),
    app.commands.getGuideStep(basics, "aspect-ratios"),
  );
  assert.equal(
    app.commands.getGuideStep(settingsGuide, "white-cap"),
    app.commands.getGuideStep(basics, "white-cap"),
  );
  assert.equal(settingsGuide.preparation.tutorial_printer, false);
  assert.equal(settingsGuide.preparation.tutorial_image, false);
  assert.equal(app.commands.getCatalogGuides().length, 8);
});

test("Basics accepts an already-active Tutorial Printer or a successful later selection", async () => {
  async function startWithPrinter(activePrinterId) {
    const profile = tutorialPrinterProfile();
    const printerConfigPage = fakeElement();
    printerConfigPage.classList.add("is-hidden");
    const { app } = await createFeatureHarness({
      api: {
        async prepareBasicsGuide() {
          return {
            tutorial_image: {
              filename: "Prisma Tutorial - Bubba Blanket.jpg",
              width: 1600,
              height: 1200,
              size_kb: 512,
            },
            tutorial_printer: { status: "ready", profile },
          };
        },
      },
      elements: {
        "#printerConfigPage": printerConfigPage,
      },
    });
    app.state.session.printersData = {
      printers: [
        {
          id: "bambu-x1c",
          name: "Bambu X1C",
          max_print_area: { x: 256, y: 256 },
          nozzle_profiles: [{ size: 0.2 }, { size: 0.4 }],
        },
        clone(profile),
      ],
      active_printer_id: activePrinterId,
      active_nozzle_size: 0.2,
    };
    app.commands.loadPrinters = async () => {};
    app.commands.renderImageTab = () => {};
    app.commands.updateRail = () => {};
    app.commands.renderGuideStep = () => {};
    app.commands.revealGuideTarget = () => {};
    app.commands.captureGuidePresentation = () => ({});
    app.state.settings.settingsProfiles = [{
      id: app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
      kind: "system",
      name: app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
      settings: app.commands._configSettingsProfileSnapshot(),
      modules: {},
    }];
    app.commands._doLoadSettingsProfile = async profile => {
      app.state.settings.temporarySettingsProfile = clone(profile);
      app.commands._applySettingsProfileToConfig(profile.settings);
      app.commands._setLoadedSettingsProfile(profile, { settings: profile.settings, modules: {} });
      return true;
    };
    assert.equal(await app.commands.startGuide("prisma-generator-basics"), true);
    return app;
  }

  const alreadyActive = await startWithPrinter("tutorial-printer");
  assert.equal(alreadyActive.state.guides.currentStep.id, "introduction");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "core-terminology");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "workflow");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "tutorial-nozzle");

  const selectedLater = await startWithPrinter("bambu-x1c");
  assert.equal(selectedLater.state.guides.currentStep.id, "introduction");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "core-terminology");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "workflow");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "printer-select");
  selectedLater.events.emit("printer.active-changed", {
    printerId: "bambu-x1c",
    source: "sidebar",
  });
  assert.equal(selectedLater.state.guides.currentStep.id, "printer-select");
  selectedLater.state.session.printersData.active_printer_id = "tutorial-printer";
  selectedLater.events.emit("printer.active-changed", {
    printerId: "tutorial-printer",
    source: "sidebar",
  });
  assert.equal(selectedLater.state.guides.currentStep.id, "tutorial-nozzle");
});

test("Basics preparation materializes its image and captures only fresh top suggestions", async () => {
  const prepareCalls = [];
  const profile = tutorialPrinterProfile();
  const targetFilamentCount = fakeElement();
  const targetSwapCount = fakeElement();
  const targetSuggestCount = fakeElement();
  targetFilamentCount.value = "7";
  targetSwapCount.value = "2";
  targetSuggestCount.value = "1";
  const { app } = await createFeatureHarness({
    api: {
      async prepareBasicsGuide(options = {}) {
        prepareCalls.push(options);
        return {
          tutorial_image: {
            filename: "Prisma Tutorial - Bubba Blanket.jpg",
            width: 1600,
            height: 1200,
            size_kb: 512,
          },
          tutorial_printer: { status: "ready", profile },
        };
      },
    },
    elements: {
      "#targetFilamentCount": targetFilamentCount,
      "#targetSwapCount": targetSwapCount,
      "#targetSuggestCount": targetSuggestCount,
    },
    filaments: [
      { filament_id: "eligible-a", has_profile: true },
      { filament_id: "eligible-b", has_profile: true },
      { filament_id: "excluded", has_profile: true, exclude_from_model: true },
    ],
  });
  app.state.palette.candidateSelection = new Set(["only-before-guide"]);
  app.state.palette.candidateInitialized = true;
  const selectedBeforeGuide = {
    filename: "My selected image.jpg",
    width: 800,
    height: 600,
    size_kb: 100,
  };
  app.state.image.availableImages = [selectedBeforeGuide];
  app.state.image.selectedImage = selectedBeforeGuide;
  app.commands.loadPrinters = async () => {};
  app.commands.refreshImageLibrary = async () => {
    throw new Error("Basics must not rescan the complete Image Library");
  };
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});
  app.state.settings.settingsProfiles = [{
    id: app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
    kind: "system",
    name: app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
    settings: app.commands._configSettingsProfileSnapshot(),
    modules: {},
  }];
  app.commands._doLoadSettingsProfile = async profile => {
    app.state.settings.temporarySettingsProfile = clone(profile);
    app.commands._applySettingsProfileToConfig(profile.settings);
    app.commands._setLoadedSettingsProfile(profile, { settings: profile.settings, modules: {} });
    return true;
  };
  app.commands._restoreLiveSettingsProfileState = async snapshot => {
    app.commands._applySettingsProfileToConfig(snapshot.config);
    app.state.settings.temporarySettingsProfile = clone(snapshot.temporarySettingsProfile);
    app.state.settings.loadedProfileRef = clone(snapshot.loadedProfileRef);
    app.state.settings.loadedProfileSnapshot = clone(snapshot.loadedProfileSnapshot);
  };

  assert.equal(await app.commands.startGuide("prisma-generator-basics"), true);
  assert.deepEqual(prepareCalls, [{
    includeTutorialPrinter: true,
    includeTutorialImage: true,
  }]);
  assert.equal(
    app.state.guides.runtimeContext.tutorialImageFilename,
    "Prisma Tutorial - Bubba Blanket.jpg",
  );
  assert.deepEqual(
    app.state.image.availableImages.map(image => image.filename),
    ["My selected image.jpg", "Prisma Tutorial - Bubba Blanket.jpg"],
  );
  assert.equal(app.state.image.selectedImage, selectedBeforeGuide);
  assert.equal(targetFilamentCount.value, "3");
  assert.equal(targetSwapCount.value, "0");
  assert.equal(targetSuggestCount.value, "5");
  assert.deepEqual(
    [...app.state.palette.candidateSelection].sort(),
    ["eligible-a", "eligible-b"],
  );
  assert.equal(app.state.settings.loadedProfileRef.id, "temporary-guide-basics");
  assert.equal(app.state.settings.config.image_sample_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.4);

  app.state.palette.stagingDeck = [
    { id: "old", name: "Old result" },
    { id: "new-a", name: "Fresh A" },
    { id: "new-b", name: "Fresh B" },
  ];
  const suggestionStep = app.commands
    .getGuideDefinition("prisma-generator-basics")
    .steps.find(step => step.id === "suggest-palettes");
  app.commands.captureGuideCompletion(suggestionStep, {
    cardIds: ["new-a", "new-b"],
  });

  assert.deepEqual(app.state.guides.runtimeContext.paletteA, {
    id: "new-a",
    name: "Fresh A",
  });
  assert.deepEqual(app.state.guides.runtimeContext.paletteB, {
    id: "new-b",
    name: "Fresh B",
  });

  app.commands.endGuide();
  assert.deepEqual([...app.state.palette.candidateSelection], ["only-before-guide"]);
  assert.equal(targetFilamentCount.value, "7");
  assert.equal(targetSwapCount.value, "2");
  assert.equal(targetSuggestCount.value, "1");
});

test("tutorial image upsert updates an existing selected entry without duplication", async () => {
  const { app } = await createFeatureHarness();
  const existing = {
    filename: "Prisma Tutorial - Bubba Blanket.jpg",
    width: 100,
    height: 100,
    size_kb: 1,
  };
  app.state.image.availableImages = [existing];
  app.state.image.selectedImage = existing;
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};

  const updated = app.commands.upsertImageLibraryEntry({
    filename: existing.filename,
    width: 4284,
    height: 5712,
    size_kb: 2048,
  });

  assert.equal(app.state.image.availableImages.length, 1);
  assert.equal(updated.width, 4284);
  assert.equal(updated.height, 5712);
  assert.equal(app.state.image.selectedImage, updated);
});

test("Settings Guide preparation changes only the temporary Settings Profile", async () => {
  const prepareCalls = [];
  const targetFilamentCount = fakeElement();
  targetFilamentCount.value = "8";
  const { app } = await createFeatureHarness({
    api: {
      async prepareBasicsGuide(options) {
        prepareCalls.push(options);
        return { tutorial_image: null, tutorial_printer: null };
      },
    },
    elements: { "#targetFilamentCount": targetFilamentCount },
  });
  const originalImage = { filename: "User image.jpg" };
  app.state.image.availableImages = [originalImage];
  app.state.image.selectedImage = originalImage;
  app.state.palette.candidateSelection = new Set(["user-choice"]);
  app.state.palette.candidateInitialized = true;
  app.commands.loadPrinters = async () => {
    throw new Error("Settings Guide must not reload printers");
  };
  app.commands.upsertImageLibraryEntry = () => {
    throw new Error("Settings Guide must not upsert a tutorial image");
  };
  app.state.settings.settingsProfiles = [{
    id: app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
    kind: "system",
    name: app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
    settings: app.commands._configSettingsProfileSnapshot(),
    modules: {},
  }];
  app.commands._doLoadSettingsProfile = async profile => {
    app.state.settings.temporarySettingsProfile = clone(profile);
    app.commands._setLoadedSettingsProfile(profile, { settings: profile.settings, modules: {} });
    return true;
  };
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  assert.equal(await app.commands.startGuide("settings-guide"), true);
  assert.deepEqual(prepareCalls, [{
    includeTutorialPrinter: false,
    includeTutorialImage: false,
  }]);
  assert.equal(app.state.image.selectedImage, originalImage);
  assert.deepEqual([...app.state.palette.candidateSelection], ["user-choice"]);
  assert.equal(app.state.palette.candidateInitialized, true);
  assert.equal(targetFilamentCount.value, "8");
  assert.equal(app.state.settings.loadedProfileRef.id, "temporary-guide-basics");
});

test("Basics reset and canvas gates preserve a clean tutorial image state", async () => {
  const { app } = await createFeatureHarness();
  const profile = setTutorialPrinterState(app);
  app.state.guides.runtimeContext = {
    tutorialImageFilename: "Prisma Tutorial - Bubba Blanket.jpg",
    tutorialPrinterProfile: profile,
  };
  app.state.image.selectedImage = {
    filename: "Prisma Tutorial - Bubba Blanket.jpg",
  };
  app.state.image.frameState = {
    arMode: "specified",
    widthMm: 90,
    heightMm: 120,
    scale: 100,
    rotation: 0,
    panX: 0,
    panY: 0,
    flipH: false,
    flipV: false,
  };
  app.state.image.imageAdjust = {
    mode: "color",
    exposure: 0,
    contrast: 0,
    highlight: 0,
    shadow: 0,
    tint_hue: 0,
    tint_strength: 0,
    saturation: 0,
    temperature: 0,
  };
  app.state.settings.config = { border: false };

  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-nozzle-active"), true);
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), true);
  app.state.image.frameState.arMode = "image";
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), true);
  app.state.image.frameState.arMode = "ratio";
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.image.frameState.arMode = "specified";
  app.state.image.frameState.scale = 105;
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.image.frameState.scale = 100;
  app.state.image.imageAdjust.exposure = 0.1;
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.image.imageAdjust.exposure = 0;
  app.state.settings.config.border = true;
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.settings.config.border = false;
  app.state.session.printersData.active_nozzle_size = 0.2;
  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-nozzle-active"), false);
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.session.printersData.active_nozzle_size = 0.4;
  app.state.image.frameState.widthMm = 90.5;
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);

  Object.assign(app.state.image.frameState, {
    arMode: "image",
    widthMm: 120,
    heightMm: 160,
  });
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), true);
  app.state.image.frameState.rotation = 5;
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), false);
  app.state.image.frameState.rotation = 0;
  app.state.image.imageAdjust.temperature = 0.2;
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), false);
});

test("Image-adjustment guide gate follows the active Adjustments subtab", async () => {
  const { app } = await createFeatureHarness();

  app.state.image.frameEditorTab = "size";
  assert.equal(
    app.commands.guidePredicateSatisfied("image.adjustment-tab-image"),
    false,
  );
  app.state.image.frameEditorTab = "image";
  assert.equal(
    app.commands.guidePredicateSatisfied("image.adjustment-tab-image"),
    true,
  );
});

test("Continue to Palette waits for Palette navigation and then advances automatically", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const tutorialCanvas = basics.steps.find(step => step.id === "tutorial-canvas");
  const rendered = [];
  app.commands.renderGuideStep = payload => rendered.push(payload.step.id);
  app.commands.revealGuideTarget = () => {};
  app.commands.guidePredicateSatisfied = predicateId => {
    if (predicateId === "basics.canvas-ready") return true;
    if (predicateId === "tab.creation") return app.state.ui.currentTab === "creation";
    return false;
  };
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = tutorialCanvas;
  app.state.guides.runtimeState = "running";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;
  app.state.ui.currentTab = "image";

  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "open-palette");
  assert.equal(app.state.guides.runtimeState, "waiting_for_action");
  assert.equal(app.commands.nextGuideStep(), false);
  assert.equal(app.state.guides.currentStep.id, "open-palette");

  app.events.emit("tab.changed", { tab: "image" });
  assert.equal(app.state.guides.currentStep.id, "open-palette");
  app.state.ui.currentTab = "creation";
  app.events.emit("tab.changed", { tab: "creation" });
  assert.equal(app.state.guides.currentStep.id, "palette-introduction");
  assert.deepEqual(rendered, ["open-palette", "palette-introduction"]);
});

test("Basics isolates its two tutorial solve selections and requires canonical printer context", async () => {
  const { app } = await createFeatureHarness();
  const profile = setTutorialPrinterState(app);
  app.state.guides.runtimeContext = {
    tutorialPrinterProfile: profile,
    paletteA: { id: "palette-a", name: "A" },
    paletteB: { id: "palette-b", name: "B" },
    runAId: null,
    runBId: null,
  };
  app.state.solve.solveRuns = [
    { id: "old-run", results: {} },
    { id: "run-a", results: {} },
    { id: "run-b", results: {} },
  ];
  app.state.solve.selectedRunIds = new Set(["old-run"]);
  const guide = app.commands.getGuideDefinition("prisma-generator-basics");

  app.commands.captureGuideCompletion(
    guide.steps.find(step => step.id === "solve-first"),
    { runId: "run-a", deckCardId: "palette-a" },
  );
  assert.deepEqual([...app.state.solve.selectedRunIds], ["run-a"]);

  app.commands.captureGuideCompletion(
    guide.steps.find(step => step.id === "solve-second"),
    { runId: "run-b", deckCardId: "palette-b" },
  );
  assert.deepEqual([...app.state.solve.selectedRunIds], ["run-a", "run-b"]);
  assert.equal(app.commands.guidePredicateSatisfied("basics.both-runs-selected"), true);

  app.state.solve.selectedRunIds.add("old-run");
  assert.equal(app.commands.guidePredicateSatisfied("basics.both-runs-selected"), false);
  app.state.session.printersData.active_nozzle_size = 0.2;
  assert.equal(app.commands.guidePredicateSatisfied("basics.first-solve-complete"), false);
});

test("guide controller keeps progress in memory and requires semantic success to advance", async () => {
  let remote = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  let putCalls = 0;
  const drawer = fakeElement();
  drawer.setAttribute("aria-hidden", "true");
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState(state, expectedRevision) {
      putCalls += 1;
      assert.equal(expectedRevision, remote.revision);
      remote = { ...clone(state), revision: remote.revision + 1 };
      return clone(remote);
    },
  };
  const { app } = await createFeatureHarness({
    api,
    elements: {
      "#settingsDrawer": drawer,
      "#settingsDrawerBody": fakeElement(),
      "#tabSwitch": fakeElement(),
      ".tab-content-area": fakeElement(),
    },
  });
  const rendered = [];
  app.commands.renderGuideStep = payload => rendered.push(payload.step.id);
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({ currentTab: "image" });
  app.commands.restoreGuidePresentation = () => {};

  await app.commands.loadGuideState();
  assert.equal(await app.commands.startGuide("interface-preview"), true);
  assert.equal(app.state.guides.currentStep.id, "workflow-tabs");

  app.commands.nextGuideStep();
  assert.equal(app.state.guides.runtimeState, "waiting_for_action");
  assert.equal(app.state.guides.currentStep.id, "settings-button");

  app.events.emit("settings.opened");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.state.guides.currentStep.id, "settings-button");

  app.state.settings.settingsDrawerOpen = true;
  drawer.setAttribute("aria-hidden", "false");
  app.events.emit("settings.opened");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.state.guides.currentStep.id, "white-point-rescale");
  assert.deepEqual(rendered, ["workflow-tabs", "settings-button", "white-point-rescale"]);

  app.commands.previousGuideStep();
  assert.equal(app.state.guides.currentStep.id, "settings-button");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.state.guides.runtimeState, "running");
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "white-point-rescale");
  app.commands.endGuide();
  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(app.state.guides.currentGuide, null);
  assert.equal(putCalls, 0);
  assert.deepEqual(remote, {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  });
});

test("Basics detours use a local route and return to the declared spine step", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const offer = basics.steps.find(step => step.id === "export-introduction");
  const rendered = [];
  app.commands.renderGuideStep = payload => rendered.push({
    step: payload.step.id,
    routeKind: payload.routeKind,
    stepCount: payload.stepCount,
  });
  app.commands.revealGuideTarget = () => {};
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = offer;
  app.state.guides.runtimeState = "running";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;

  assert.equal(app.commands.getOfferedGuideDetour().id, "export-choices");
  assert.equal(app.commands.startGuideDetour("export-choices"), true);
  assert.equal(app.state.guides.currentStep.id, "export-choices-overview");
  assert.deepEqual(rendered.at(-1), {
    step: "export-choices-overview",
    routeKind: "detour",
    stepCount: 1,
  });
  assert.deepEqual(app.commands.getGuideProgressModel(), {
    label: "Export Choices",
    count: "Optional · 1 of 1",
    value: 1,
    max: 1,
    valueText: "Export Choices, optional step 1 of 1",
    mode: "detour",
    segments: [{
      id: "export-choices",
      label: "Export Choices",
      state: "active",
      fraction: 1,
    }],
  });

  const runtimeContext = { retained: true };
  app.state.guides.runtimeContext = runtimeContext;
  assert.equal(app.commands.previousGuideStep(), false);
  assert.equal(app.commands.exitGuideDetour(), true);
  assert.equal(app.state.guides.currentStep.id, "export-introduction");
  assert.equal(app.state.guides.activeDetour, null);
  assert.equal(app.state.guides.currentGuide, basics);
  assert.equal(app.state.guides.runtimeContext, runtimeContext);
  assert.equal(app.commands.startGuideDetour("export-choices"), true);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "select-export-run");
  assert.equal(app.state.guides.activeDetour, null);
  assert.equal(app.state.guides.completedDetourIds.has("export-choices"), true);
});

test("a detour return predicate blocks invalid state before rejoining the spine", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const detour = app.commands.getGuideDetour(basics, "settings-tools");
  const warnings = [];
  let ready = false;
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.showToast = message => warnings.push(message);
  app.commands.guidePredicateSatisfied = predicateId => (
    predicateId === "basics.settings-detour-ready" ? ready : true
  );
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = detour.steps.at(-1);
  app.state.guides.runtimeState = "waiting_for_action";
  app.state.guides.completedStepIds = new Set([detour.steps.at(-1).id]);
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = { id: detour.id };

  assert.equal(app.commands.nextGuideStep(), false);
  assert.equal(app.state.guides.currentStep.id, "advanced-settings");
  assert.match(warnings.at(-1), /restore the requested tutorial state/i);
  ready = true;
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "close-settings");
  assert.equal(app.state.guides.activeDetour, null);
});

test("chapter progress is reusable for multi-chapter and one-chapter guides", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = basics.steps.find(step => step.id === "choose-image");
  const basicsProgress = app.commands.getGuideProgressModel();
  assert.equal(basicsProgress.label, "Image");
  assert.equal(basicsProgress.mode, "spine");
  assert.equal(basicsProgress.segments.length, 6);
  assert.equal(basicsProgress.segments[0].state, "complete");
  assert.equal(basicsProgress.segments[1].state, "active");

  const setup = app.commands.getGuideDefinition("guided-setup");
  app.state.guides.currentGuide = setup;
  app.state.guides.currentStep = setup.steps[2];
  const setupProgress = app.commands.getGuideProgressModel();
  assert.equal(setupProgress.label, "Setup");
  assert.equal(setupProgress.count, "3 of 9");
  assert.equal(setupProgress.segments.length, 1);
  assert.equal(setupProgress.segments[0].fraction, 3 / 9);
});

test("verified completion waits for a blocking progress surface to close before advancing", async () => {
  const drawer = fakeElement();
  drawer.setAttribute("aria-hidden", "true");
  const { app } = await createFeatureHarness({
    elements: { "#settingsDrawer": drawer },
  });
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.startGuide("interface-preview");
  app.commands.nextGuideStep();
  assert.equal(app.state.guides.currentStep.id, "settings-button");
  app.state.settings.settingsDrawerOpen = true;
  drawer.setAttribute("aria-hidden", "false");
  app.commands.setGuideSuspended(true);
  app.events.emit("settings.opened");

  assert.equal(app.state.guides.currentStep.id, "settings-button");
  assert.equal(app.state.guides.completedStepIds.has("settings-button"), true);
  app.commands.setGuideSuspended(false);
  assert.equal(app.state.guides.currentStep.id, "white-point-rescale");
});

test("guide completion restores presentation without persisting guide progress", async () => {
  let remote = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  let putCalls = 0;
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState(state, expectedRevision) {
      putCalls += 1;
      assert.equal(expectedRevision, remote.revision);
      remote = { ...clone(state), revision: remote.revision + 1 };
      return clone(remote);
    },
  };
  const { app } = await createFeatureHarness({ api });
  const restored = [];
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({ currentTab: "creation" });
  app.commands.restoreGuidePresentation = snapshot => restored.push(snapshot);

  await app.commands.loadGuideState();
  await app.commands.startGuide("interface-preview");
  const guide = app.commands.getGuideDefinition("interface-preview");
  app.state.guides.currentStep = guide.steps.at(-1);

  assert.equal(app.commands.nextGuideStep(), true);
  assert.deepEqual(restored, [{ currentTab: "creation" }]);
  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(putCalls, 0);
  assert.equal(remote.revision, 0);
});

test("only first-launch status is persisted", async () => {
  let remote = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState(state, expectedRevision) {
      assert.equal(expectedRevision, remote.revision);
      remote = { ...clone(state), revision: remote.revision + 1 };
      return clone(remote);
    },
  };
  const { app } = await createFeatureHarness({ api });
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.persistWelcomeStatus("declined"), true);
  assert.deepEqual(remote, {
    schema_version: 2,
    revision: 1,
    welcome_status: "declined",
  });
  assert.deepEqual(app.state.guides.onboardingState, remote);
});

test("first launch accepts Guided Setup and records the response before starting", async () => {
  let remote = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState(state, expectedRevision) {
      assert.equal(expectedRevision, remote.revision);
      remote = { ...clone(state), revision: remote.revision + 1 };
      return clone(remote);
    },
  };
  const printerPage = fakeElement();
  printerPage.classList.add("is-hidden");
  const { app } = await createFeatureHarness({
    api,
    elements: { "#printerConfigPage": printerPage },
  });
  app.commands.appConfirm = async () => true;
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), true);

  assert.equal(remote.welcome_status, "accepted");
  assert.equal(remote.revision, 1);
  assert.equal(app.state.guides.currentGuide.id, "guided-setup");
  assert.equal(app.state.guides.currentStep.id, "printer-open");
});

test("Guided Setup reviews a completed printer step without reopening its editor", async () => {
  const printerPage = fakeElement();
  const { app } = await createFeatureHarness({
    elements: { "#printerConfigPage": printerPage },
  });
  const rendered = [];
  let reopened = 0;
  app.commands.renderGuideStep = payload => rendered.push(payload.step.id);
  app.commands.hideGuideOverlay = () => {};
  app.commands.captureGuidePresentation = () => ({});
  app.commands.showPrinterConfigPage = () => {
    reopened += 1;
    printerPage.classList.remove("is-hidden");
    app.events.emit("printer-config.opened", { source: "test" });
  };

  assert.equal(await app.commands.startGuide("guided-setup"), true);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");
  assert.deepEqual(rendered, ["printer-open", "printer-configuration"]);

  printerPage.classList.add("is-hidden");
  app.events.emit("printer-config.closed", { source: "test" });
  assert.equal(app.state.guides.currentStep.id, "active-nozzle");

  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(reopened, 0);
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "active-nozzle");
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.commands.previousGuideStep(), false);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");
});

test("Not Now records the response and its Help pointer dismisses on the next click", async () => {
  let remote = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState(state) {
      remote = { ...clone(state), revision: remote.revision + 1 };
      return clone(remote);
    },
  };
  const documentTarget = Object.assign(eventTarget(), {
    activeElement: null,
    body: fakeElement(),
    documentElement: { clientWidth: 1000, clientHeight: 700 },
  });
  const viewportTarget = Object.assign(eventTarget(), {
    getComputedStyle: () => ({ overflow: "visible", overflowX: "visible", overflowY: "visible" }),
    innerWidth: 1000,
    innerHeight: 700,
    location: { search: "" },
    requestAnimationFrame: () => 1,
    setTimeout,
    clearTimeout,
    visualViewport: null,
  });
  const helpButton = menuElement(documentTarget);
  const helpMenu = menuElement(documentTarget);
  const { app } = await createFeatureHarness({
    api,
    elements: {
      "#helpGuidesMenuBtn": helpButton,
      "#helpGuidesMenu": helpMenu,
      "#guideOverlayRoot": fakeElement(),
      "#guideStepCard": fakeElement(),
      "#guideTargetFrame": fakeElement(),
    },
  });
  app.commands.appConfirm = async () => false;
  app.commands.initializeGuidesController({
    viewport: viewportTarget,
    documentEvents: documentTarget,
    forceGuidedSetup: false,
  });

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), true);
  assert.equal(remote.welcome_status, "declined");
  assert.equal(app.state.guides.currentGuide.id, "guided-setup-help-pointer");

  documentTarget.dispatch("click", { target: helpButton });
  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(app.state.guides.currentGuide, null);
});

test("forced Guided Setup bypasses and does not mutate saved first-launch state", async () => {
  let putCalls = 0;
  const remote = {
    schema_version: 2,
    revision: 12,
    welcome_status: "declined",
  };
  const api = {
    async fetchGuideState() { return clone(remote); },
    async putGuideState() {
      putCalls += 1;
      return clone(remote);
    },
  };
  const { app } = await createFeatureHarness({ api });
  app.state.guides.forceGuidedSetup = true;
  app.commands.appConfirm = async () => true;
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), true);
  assert.equal(putCalls, 0);
  assert.deepEqual(app.state.guides.onboardingState, remote);
  assert.equal(app.state.guides.currentGuide.id, "guided-setup");
});

test("a stale first-launch writer respects the canonical response from another tab", async () => {
  let reads = 0;
  const api = {
    async fetchGuideState() {
      reads += 1;
      return reads === 1
        ? { schema_version: 2, revision: 0, welcome_status: "not_offered" }
        : { schema_version: 2, revision: 1, welcome_status: "accepted" };
    },
    async putGuideState() {
      const error = new Error("stale");
      error.status = 409;
      throw error;
    },
  };
  const { app } = await createFeatureHarness({ api });
  app.commands.appConfirm = async () => false;
  app.commands.showToast = () => {};
  app.commands.renderGuideStep = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), false);
  assert.equal(app.state.guides.currentGuide, null);
  assert.equal(app.state.guides.onboardingState.welcome_status, "accepted");
});

test("a stale first-launch writer does not act locally when conflict refresh fails", async () => {
  let reads = 0;
  const api = {
    async fetchGuideState() {
      reads += 1;
      if (reads > 1) throw new Error("refresh unavailable");
      return { schema_version: 2, revision: 0, welcome_status: "not_offered" };
    },
    async putGuideState() {
      const error = new Error("stale");
      error.status = 409;
      throw error;
    },
  };
  const { app } = await createFeatureHarness({ api });
  app.commands.appConfirm = async () => true;
  app.commands.showToast = () => {};
  app.commands.renderGuideStep = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), false);
  assert.equal(app.state.guides.currentGuide, null);
  assert.equal(app.state.guides.onboardingState.welcome_status, "not_offered");
});

test("a first-launch save outage still honors the user's in-session choice", async () => {
  const printerPage = fakeElement();
  printerPage.classList.add("is-hidden");
  const api = {
    async fetchGuideState() {
      return { schema_version: 2, revision: 0, welcome_status: "not_offered" };
    },
    async putGuideState() {
      throw new Error("storage unavailable");
    },
  };
  const { app } = await createFeatureHarness({
    api,
    elements: { "#printerConfigPage": printerPage },
  });
  app.commands.appConfirm = async () => true;
  app.commands.showToast = () => {};
  app.commands.renderGuideStep = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), true);
  assert.equal(app.state.guides.currentGuide.id, "guided-setup");
  assert.equal(app.state.guides.currentStep.id, "printer-open");
});

test("first-launch welcome waits for Model Library recovery to close", async () => {
  const recoveryModal = fakeElement();
  recoveryModal.setAttribute("aria-hidden", "false");
  const { app } = await createFeatureHarness({
    api: {
      async fetchGuideState() {
        return { schema_version: 2, revision: 0, welcome_status: "not_offered" };
      },
      async putGuideState(state) {
        return { ...clone(state), revision: 1 };
      },
    },
    elements: { "#modelLibrariesModal": recoveryModal },
  });
  let confirmations = 0;
  app.commands.appConfirm = async () => {
    confirmations += 1;
    return true;
  };
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.loadGuideState();
  assert.equal(await app.commands.maybeOfferGuidedSetup(), false);
  assert.equal(confirmations, 0);

  recoveryModal.setAttribute("aria-hidden", "true");
  app.events.emit("model-libraries.closed");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(confirmations, 1);
  assert.equal(app.state.guides.currentGuide.id, "guided-setup");
});

test("opening the Guides Library ends a running guide after confirmation", async () => {
  const catalog = fakeElement();
  const catalogClose = fakeElement();
  const helpButton = fakeElement();
  const { app } = await createFeatureHarness({
    elements: {
      "#guidesCatalogModal": catalog,
      "#guidesCatalogClose": catalogClose,
      "#helpGuidesMenuBtn": helpButton,
    },
  });
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({ currentTab: "image" });
  const restored = [];
  app.commands.restoreGuidePresentation = snapshot => restored.push(snapshot);
  app.commands.appConfirm = async () => true;

  await app.commands.startGuide("interface-preview");
  assert.equal(await app.commands.openGuidesCatalog(), true);

  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(app.state.guides.currentGuide, null);
  assert.deepEqual(restored, [{ currentTab: "image" }]);
  assert.equal(catalog.getAttribute("aria-hidden"), "false");
});

test("starting a guide from the catalog restores focus before hiding the dialog", async () => {
  const catalog = fakeElement();
  catalog.setAttribute("aria-hidden", "false");
  let helpFocusCount = 0;
  const helpButton = fakeElement();
  helpButton.focus = () => { helpFocusCount += 1; };
  const { app } = await createFeatureHarness({
    elements: {
      "#guidesCatalogModal": catalog,
      "#helpGuidesMenuBtn": helpButton,
    },
  });
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.captureGuidePresentation = () => ({});

  assert.equal(await app.commands.startGuide("interface-preview"), true);
  assert.equal(catalog.getAttribute("aria-hidden"), "true");
  assert.equal(helpFocusCount, 1);
});

test("target-unavailable recovery retains the step's prior runtime state", async () => {
  const { app } = await createFeatureHarness();
  const rendered = [];
  app.commands.renderGuideStep = payload => rendered.push(payload.step.id);
  app.commands.hideGuideOverlay = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});

  await app.commands.startGuide("interface-preview");
  app.commands.handleGuideTargetUnavailable("workflow.tabs");
  assert.equal(app.state.guides.runtimeState, "target_unavailable");

  app.commands.handleGuideTargetAvailable();
  assert.equal(app.state.guides.runtimeState, "running");
  assert.deepEqual(rendered, ["workflow-tabs", "workflow-tabs"]);
});

test("guide targets are resolved again after DOM replacement", async () => {
  const selector = '[data-guide-target="settings.white-point-rescale"]';
  const first = fakeElement();
  const second = fakeElement();
  const elements = { [selector]: first };
  const { app } = await createFeatureHarness({ elements });

  assert.equal(app.commands.resolveGuideTarget("settings.white-point-rescale"), first);
  elements[selector] = second;
  assert.equal(app.commands.resolveGuideTarget("settings.white-point-rescale"), second);
});

test("a grouped guide target resolves separate spotlight regions after rerenders", async () => {
  const { app } = await createFeatureHarness();
  const firstCanvas = fakeElement();
  const secondCanvas = fakeElement();
  const scale = fakeElement();
  const rotation = fakeElement();
  let canvas = firstCanvas;
  app.state.ui.$ = selector => (
    selector === "#frameCanvasWrap" ? canvas : null
  );
  app.state.ui.$$ = selector => (
    selector === '[data-guide-target-part="image.transform-controls"]'
      ? [scale, rotation]
      : []
  );

  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.framing"),
    [[firstCanvas], [scale, rotation]],
  );
  canvas = secondCanvas;
  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.framing"),
    [[secondCanvas], [scale, rotation]],
  );
});

test("the workflow overview frames Solve and Settings as one utility region", async () => {
  const { app } = await createFeatureHarness();
  const tabs = fakeElement();
  const solve = fakeElement();
  const settings = fakeElement();
  app.state.ui.$ = selector => (
    selector === '[data-guide-target="workflow.tabs"]' ? tabs : null
  );
  app.state.ui.$$ = selector => (
    selector === '#solveActionSplit, [data-guide-target="topbar.settings"]'
      ? [solve, settings]
      : []
  );

  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("workflow.overview"),
    [[tabs], [solve, settings]],
  );
});

test("adjacent Image controls share semantic frames while library actions stay distinct", async () => {
  const { app } = await createFeatureHarness();
  const canvas = fakeElement();
  const direction = fakeElement();
  const ratios = fakeElement();
  const library = fakeElement();
  const libraryActions = fakeElement();
  app.state.ui.$ = selector => ({
    "#imageLibraryPanel": library,
    ".library-title-actions": libraryActions,
  })[selector] || null;
  app.state.ui.$$ = selector => ({
    "#frameCanvasWrap, #directionToggle, #arButtonGroup": [canvas, direction, ratios],
  })[selector] || [];

  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.aspect-experiment"),
    [[canvas, direction, ratios]],
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.library-management"),
    [[library], [libraryActions]],
  );
});

test("guide target scrolling preserves a visible target's horizontal position", async () => {
  const { scrollGuideTargetIntoView } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const calls = [];
  scrollGuideTargetIntoView({
    scrollIntoView(options) {
      calls.push(options);
    },
  });

  assert.deepEqual(calls, [{
    block: "center",
    inline: "nearest",
    behavior: "auto",
  }]);
});

test("grouped guide target scrolling reveals both ends of a region", async () => {
  const { scrollGuideRegionIntoView } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const calls = [];
  const element = name => ({
    scrollIntoView(options) {
      calls.push({ name, options });
    },
  });

  scrollGuideRegionIntoView([
    element("first"),
    element("middle"),
    element("last"),
  ], { block: "center" });

  assert.deepEqual(calls, [
    {
      name: "first",
      options: { block: "center", inline: "nearest", behavior: "auto" },
    },
    {
      name: "last",
      options: { block: "nearest", inline: "nearest", behavior: "auto" },
    },
  ]);
});

test("guide overlays hide unused frames after a multi-target step", async () => {
  const { syncGuideOverlayElementVisibility } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const first = fakeElement();
  const second = fakeElement();

  syncGuideOverlayElementVisibility([first, second], 2);
  assert.equal(first.classList.contains("is-hidden"), false);
  assert.equal(second.classList.contains("is-hidden"), false);
  syncGuideOverlayElementVisibility([first, second], 1);
  assert.equal(first.classList.contains("is-hidden"), false);
  assert.equal(second.classList.contains("is-hidden"), true);
});

test("Previous stays within a route and is absent on its first step", async () => {
  const { guidePreviousDisabled, guidePreviousHidden } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  assert.equal(guidePreviousDisabled({
    stepIndex: 0,
    routeKind: "detour",
    allowPrevious: true,
  }), true);
  assert.equal(guidePreviousDisabled({
    stepIndex: 0,
    routeKind: "spine",
    allowPrevious: true,
  }), true);
  assert.equal(guidePreviousDisabled({
    stepIndex: 2,
    routeKind: "detour",
    allowPrevious: true,
  }), false);
  assert.equal(guidePreviousDisabled({
    stepIndex: 2,
    routeKind: "detour",
    allowPrevious: false,
  }), true);
  assert.equal(guidePreviousHidden({ stepIndex: 0, unavailable: false }), true);
  assert.equal(guidePreviousHidden({ stepIndex: 1, unavailable: false }), false);
  assert.equal(guidePreviousHidden({ stepIndex: 1, unavailable: true }), true);
});

test("standalone guide cards are centered within the visual viewport", async () => {
  const { centerStandaloneGuideCard } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  assert.deepEqual(
    centerStandaloneGuideCard({
      cardSize: { width: 320, height: 180 },
      viewportRect: {
        left: 100,
        top: 50,
        right: 1100,
        bottom: 750,
        width: 1000,
        height: 700,
      },
    }),
    {
      left: 440,
      top: 310,
      placement: "center",
      docked: false,
    },
  );

  const oversized = centerStandaloneGuideCard({
    cardSize: { width: 4000, height: 4000 },
    viewportRect: {
      left: 100,
      top: 50,
      right: 1100,
      bottom: 750,
      width: 1000,
      height: 700,
    },
  });
  assert.deepEqual(oversized, {
    left: 108,
    top: 58,
    placement: "center",
    docked: false,
  });
});

test("dialogs, top-bar menus, and operation progress temporarily block the guide overlay", async () => {
  const { guideBlockingDialogOpen } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const modal = fakeElement();
  modal.classList.add("modal-overlay");
  const menu = fakeElement();
  const progress = fakeElement();
  menu.hidden = true;
  progress.classList.add("is-hidden");
  const app = {
    state: {
      ui: {
        $: selector => {
          if (selector === '.modal-overlay[aria-hidden="false"]') return modal;
          if (selector === ".topbar-menu:not([hidden])") return menu.hidden ? null : menu;
          if (selector === "#opProgress:not(.is-hidden)") {
            return progress.classList.contains("is-hidden") ? null : progress;
          }
          return null;
        },
      },
    },
  };

  modal.setAttribute("aria-hidden", "true");
  assert.equal(guideBlockingDialogOpen(app), false);
  modal.setAttribute("aria-hidden", "false");
  assert.equal(guideBlockingDialogOpen(app), true);
  modal.classList.remove("modal-overlay");
  assert.equal(guideBlockingDialogOpen(app), false);
  menu.hidden = false;
  assert.equal(guideBlockingDialogOpen(app), true);
  menu.hidden = true;
  progress.classList.remove("is-hidden");
  assert.equal(guideBlockingDialogOpen(app), true);
});

test("guide markup keeps targets inert and overlay pointer handling isolated", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(appDir, "styles", "guides.css"), "utf8");
  const components = fs.readFileSync(path.join(appDir, "styles", "components.css"), "utf8");
  const tokens = fs.readFileSync(path.join(appDir, "styles", "tokens.css"), "utf8");
  const definitions = fs.readFileSync(
    path.join(appDir, "features", "guides", "definitions.js"),
    "utf8",
  );
  const targets = fs.readFileSync(
    path.join(appDir, "features", "guides", "targets.js"),
    "utf8",
  );

  for (const target of [
    "sidebar.printer",
    "printer.configuration",
    "sidebar.active-nozzle",
    "sidebar.model-library",
    "sidebar.active-filaments",
    "topbar.theme",
    "topbar.clear-temp",
    "topbar.help-guides",
    "workflow.tabs",
    "topbar.settings",
    "settings.white-point-rescale",
    "image.library",
  ]) {
    assert.match(html, new RegExp(`data-guide-target="${target.replace(".", "\\.")}"`));
  }
  assert.match(html, /id="helpGuidesMenuBtn"[\s\S]*?aria-label="Help &amp; Guides"/);
  assert.match(
    html,
    /class="workflow-tab-strip"\s+data-guide-target="workflow\.tabs"/,
  );
  assert.doesNotMatch(
    html,
    /id="tabSwitch"\s+data-guide-target="workflow\.tabs"/,
  );
  assert.match(css, /\.guide-overlay-root\s*{[\s\S]*?pointer-events:\s*none;/);
  assert.match(css, /\.guide-overlay-shade\s*{[\s\S]*?background:\s*var\(--scrim\);[\s\S]*?pointer-events:\s*none;/);
  assert.match(css, /\.guide-target-frame\s*{[\s\S]*?pointer-events:\s*none;/);
  assert.match(css, /\.guide-step-card\s*{[\s\S]*?pointer-events:\s*auto;/);
  assert.match(
    html,
    /class="guide-step-card surface-window"[\s\S]*?class="guide-step-header window-header surface-header"[\s\S]*?class="guide-step-content surface-body"[\s\S]*?class="guide-step-actions surface-footer"/,
  );
  assert.match(
    html,
    /id="guideStepTitle"[^>]*class="window-header__title surface-title"|class="window-header__title surface-title"[^>]*id="guideStepTitle"/,
  );
  assert.match(
    css,
    /\.guide-step-content\s*{[\s\S]*?background:\s*var\(--panel\);/,
  );
  assert.match(
    css,
    /\.guide-step-actions\s*{[\s\S]*?background:\s*var\(--surface-raised\);/,
  );
  assert.match(tokens, /--z-guide-popover:\s*4100;/);
  assert.match(
    css,
    /\.guide-overlay-root:not\(\.is-hidden\):not\(\.is-suspended\)\s*~\s*\.topbar-menu\s*{[\s\S]*?z-index:\s*var\(--z-guide-popover\);/,
  );
  assert.doesNotMatch(targets, /classList\.(?:add|remove|toggle)/);
  assert.doesNotMatch(targets, /\.style\./);
  assert.match(
    targets,
    /"workflow\.overview": groupedTarget\(\[[\s\S]*?selector:\s*'#solveActionSplit, \[data-guide-target="topbar\.settings"\]'[\s\S]*?all:\s*true/,
  );
  assert.match(html, /id="helpGuidesMenuBtn"[\s\S]*?aria-haspopup="menu"[\s\S]*?aria-expanded="false"[\s\S]*?aria-controls="helpGuidesMenu"/);
  assert.match(html, /id="guideStepBody"[^>]*aria-live="polite"/);
  assert.match(css, /\.guide-step-body\s*{[^}]*white-space:\s*pre-line;/);
  assert.doesNotMatch(html, /id="guideStepCounter"/);
  assert.match(
    html,
    /class="guide-step-actions surface-footer"[\s\S]*?id="guideProgress"[\s\S]*?id="guideProgressTrack"[\s\S]*?role="progressbar"/,
  );
  assert.match(html, /id="guideStepDetour"[\s\S]*?id="guideStepDetourButton"/);
  assert.match(css, /\.guide-progress\s*{[\s\S]*?border-top:/);
  assert.match(css, /\.guide-progress__segment::after\s*{[\s\S]*?width:\s*var\(--guide-progress-width, 0%\);/);
  assert.match(css, /\.guide-step-detour\s*{/);
  assert.match(
    css,
    /\.guide-step-detour\s*{[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) auto;/,
  );
  assert.match(
    css,
    /\.guides-catalog-window\s*{[\s\S]*?display:\s*flex;[\s\S]*?flex-direction:\s*column;[\s\S]*?width:\s*min\(920px, 92vw\);[\s\S]*?max-width:\s*min\(920px, 92vw\);[\s\S]*?max-height:\s*90vh;[\s\S]*?overflow:\s*hidden;/,
  );
  assert.match(
    css,
    /\.guides-catalog-body\s*{[\s\S]*?min-height:\s*0;[\s\S]*?flex:\s*1 1 auto;[\s\S]*?overflow-y:\s*auto;/,
  );
  assert.match(
    html,
    /id="guidesCatalogTitle"[\s\S]*?class="surface-window-controls"[\s\S]*?id="guidesCatalogClose"/,
  );
  assert.match(components, /\.summary-action-grid\s*{[\s\S]*?grid-template-columns:/);
  assert.match(components, /\.summary-action-row\s*{[\s\S]*?grid-template-columns:/);
  assert.doesNotMatch(css, /\.guide-catalog-entry\b/);
  assert.doesNotMatch(css, /var\(--ink-soft\)/);
  assert.doesNotMatch(definitions, /action_label/);
  assert.match(
    html,
    /id="ratioDialog"[^>]*role="dialog"[^>]*aria-modal="true"[^>]*aria-labelledby="ratioDialogTitle"/,
  );
  assert.match(
    html,
    /class="frame-tab"[^>]*data-ftab="image"[^>]*data-guide-target="image\.adjustment-image-tab"/,
  );
  assert.equal(
    (html.match(/data-guide-target-part="image\.transform-controls"/g) || []).length,
    7,
  );
  assert.doesNotMatch(html, /id="(?:helpGuidesContinue|guideStepPause)"/);
  const controller = fs.readFileSync(
    path.join(appDir, "features", "guides", "controller.js"),
    "utf8",
  );
  assert.match(controller, /button\.textContent\s*=\s*"Start"/);
  assert.match(controller, /setAttribute\("aria-label", `Start \$\{guide\.title\}`\)/);
  assert.match(
    controller,
    /FIRST_RUN_GUIDE_OFFER_ENABLED\s*=\s*true/,
  );
  assert.doesNotMatch(controller, /\b(?:active_guide|completed_guides|pauseGuide|resumeGuide)\b/);
  const utilities = fs.readFileSync(path.join(appDir, "styles", "utilities.css"), "utf8");
  assert.match(utilities, /\.topbar-menu \[role="menuitem"\]\[hidden\][\s\S]*?display:\s*none;/);
  assert.doesNotMatch(css, /transition:\s*(?:left|top|width|height)/);
});
