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

async function settleGuideStepActions(app) {
  for (let attempt = 0; attempt < 20 && app.state.guides.runtimeState === "preparing"; attempt += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
}

function tutorialPrinterProfile() {
  return {
    id: "tutorial-printer",
    name: "Tutorial Printer",
    max_print_area: { x: 256, y: 256 },
    nozzle_profiles: [
      { id: "nozzle-200", diameter_um: 200, min_layer_height_um: 50, max_layer_height_um: 150, max_extrusion_width_um: 250, minimum_line_length_multiplier: 2 },
      { id: "nozzle-400", diameter_um: 400, min_layer_height_um: 80, max_layer_height_um: 320, max_extrusion_width_um: 500, minimum_line_length_multiplier: 2 },
    ],
  };
}

function setTutorialPrinterState(app, profile = tutorialPrinterProfile(), widthUm = 400) {
  const stored = clone(profile);
  app.state.session.printersData = {
    schema_version: 3,
    revision: 1,
    printers: [stored],
    active_printer_id: "tutorial-printer",
    printer_setup_state: {
      "tutorial-printer": {
        active_nozzle_id: widthUm === 200 ? "nozzle-200" : "nozzle-400",
        nozzle_width_state: {
          "nozzle-200": { current_width_um: 200, saved_widths_um: [200] },
          "nozzle-400": { current_width_um: 400, saved_widths_um: [400] },
        },
      },
    },
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

test("guide emphasis renders strong and italic text without interpreting arbitrary markup", async () => {
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

  renderGuideTextContent(element, "A **palette**, a **solve**, and *potential* <remain text>.");

  assert.deepEqual(
    element.children.map(child => [child.tagName || "TEXT", child.textContent]),
    [
      ["TEXT", "A "],
      ["STRONG", "palette"],
      ["TEXT", ", a "],
      ["STRONG", "solve"],
      ["TEXT", ", and "],
      ["EM", "potential"],
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
  const adjustmentsPlacement = chooseGuideCardPlacement({
    targetRect: {
      left: 1630,
      top: 52,
      right: 2035,
      bottom: 960,
      width: 405,
      height: 908,
    },
    cardSize: { width: 380, height: 420 },
    viewportRect: {
      left: 0,
      top: 0,
      right: 2048,
      bottom: 969,
      width: 2048,
      height: 969,
    },
    preferred: ["left", "bottom", "top", "right"],
    avoidRects: [
      { left: 255, top: 228, right: 1625, bottom: 960 },
      { left: 1630, top: 52, right: 2035, bottom: 960 },
    ],
  });
  assert.equal(adjustmentsPlacement.placement, "left");
  assert.equal(adjustmentsPlacement.left, 1240);
  assert.equal(chooseGuideCardPlacement({
    targetRect: { left: 0, top: 100, right: 1000, bottom: 700 },
    cardSize: { width: 260, height: 140 },
    viewportRect,
    avoidRects: [
      { left: 650, top: 0, right: 1000, bottom: 700 },
      { left: 0, top: 220, right: 650, bottom: 700 },
    ],
  }).placement, "dock-top-left");
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

  third.dataset.menuStayOpen = "true";
  controller.open();
  menu.dispatch("click", { target: third });
  assert.deepEqual(activated, [second, third]);
  assert.equal(menu.hidden, false);
  controller.close();

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
  assert.equal(guidedSetup.title, "First-Time Setup");
  assert.deepEqual(
    guidedSetup.steps.map(step => step.id),
    [
      "printer-open",
      "printer-configuration",
      "active-extrusion-width",
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
  assert.equal(guidedSetup.steps[0].viewport_anchor, "center");
  assert.equal(guidedSetup.steps[1].completion.event, "printer-config.closed");
  assert.equal(guidedSetup.steps[1].allow_previous, true);
  assert.equal(guidedSetup.steps[2].title, "Changing the Extrusion Width");
  assert.equal(guidedSetup.steps.at(-1).target_id, null);
  assert.equal(
    guidedSetup.steps.at(-1).followup.text,
    "If you are new to Prisma Generator, you may want to continue with a guided introduction to its features and workflow.",
  );
  const modelLibraryStep = guidedSetup.steps.find(step => step.id === "model-library");
  assert.match(modelLibraryStep.body, /change the active library/i);
  assert.match(modelLibraryStep.body, /do not change it until you finish this guide/i);
  assert.doesNotMatch(modelLibraryStep.body, /install and select/i);
  assert.equal(
    app.commands.getGuideDefinition("guided-setup-help-pointer").steps[0].completion.kind,
    "interaction",
  );
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  assert.equal(basics.workspace_policy, "basic-teaching");
  assert.equal(basics.canonical_guide_id, "prisma-generator-basics");
  assert.equal(basics.route_id, "full");
  assert.deepEqual(Object.keys(basics.routes), ["full", "image", "palette", "settings", "preview", "export"]);
  assert.deepEqual(app.commands.resolveGuideLaunch("image-guide"), {
    guide_id: "prisma-generator-basics",
    route_id: "image",
  });
  assert.equal(basics.version, 19);
  assert.equal(basics.steps.length, 42);
  assert.ok(basics.steps.length <= 42);
  assert.ok(basics.steps.findIndex(step => step.id === "solve-first") <= 29);
  assert.deepEqual(
    basics.chapters.map(current => current.label),
    ["Introduction", "Image", "Palette", "Settings", "Preview", "Export"],
  );
  assert.deepEqual(
    basics.detours.map(current => current.id),
    ["image-controls", "palette-tools", "settings-tools", "preview-tools", "export-choices"],
  );
  for (const currentDetour of basics.detours) {
    assert.equal(
      currentDetour.return_step_id,
      currentDetour.offer_step_id,
      `${currentDetour.id} should return to its offering step`,
    );
  }
  assert.equal(basics.steps[0].id, "introduction");
  assert.equal(basics.steps[0].target_id, null);
  assert.equal(basics.steps[0].overlay_mode, "full-scrim");
  assert.equal(basics.steps[0].next_label, "Begin");
  assert.equal(basics.steps[0].allow_previous, false);
  assert.equal(basics.steps[1].id, "core-terminology");
  assert.equal(basics.steps[1].title, "Terminology");
  assert.equal(basics.steps[1].target_id, null);
  assert.match(basics.steps[1].body, /• A \*\*palette\*\*/);
  assert.match(basics.steps[1].body, /• A \*\*solve\*\*/);
  assert.equal(basics.steps[2].id, "lithophane-image-principle");
  assert.equal(basics.steps[2].title, "How a lithophane creates an image");
  assert.equal(basics.steps[2].target_id, null);
  assert.equal(basics.steps[2].overlay_mode, "full-scrim");
  assert.equal(basics.steps[3].id, "prisma-creation-overview");
  assert.equal(basics.steps[3].title, "How Prisma turns an image into a print");
  assert.equal(basics.steps[3].target_id, null);
  assert.equal(basics.steps[3].overlay_mode, "full-scrim");
  assert.equal(basics.steps[4].id, "workflow");
  assert.equal(basics.steps[4].target_id, "workflow.overview");
  assert.equal(basics.steps[4].viewport_anchor, "center");
  assert.match(
    basics.steps[4].body,
    /Image → Palette → Preview → Export\n\nSettings[\s\S]*\n\nEach stage/,
  );
  assert.equal(basics.steps[5].id, "printer-select");
  assert.equal(basics.steps[5].target_id, "sidebar.active-printer");
  assert.equal(basics.steps[5].completion.event, "printer.active-changed");
  assert.equal(basics.steps[5].completion.accept_preexisting, true);
  assert.equal(basics.steps[5].viewport_anchor, "center");
  assert.equal(basics.steps[6].id, "tutorial-extrusion-width");
  assert.equal(basics.steps[6].title, "Extrusion Width selection");
  assert.equal(basics.steps[6].viewport_anchor, "center");
  assert.equal(basics.steps[6].completion.kind, "event");
  assert.equal(basics.steps[6].completion.event, "printer.extrusion-width-changed");
  assert.equal(basics.steps[6].completion.accept_preexisting, false);
  assert.deepEqual(basics.steps[6].enter_actions, [
    { action: "printer.select_print_setup", input: { nozzle_id: "nozzle-200", extrusion_width_um: 200 } },
  ]);
  assert.equal(basics.steps[5].placement_group, "basics-printer");
  assert.equal(basics.steps[6].placement_group, "basics-printer");
  for (const stepId of [
    "introduction",
    "palette-introduction",
    "settings-introduction",
    "preview-introduction",
    "export-introduction",
  ]) {
    const chapterStep = basics.steps.find(step => step.id === stepId);
    assert.equal(chapterStep.target_id, null);
    assert.equal(chapterStep.completion.kind, "manual");
    assert.equal(chapterStep.placement_group, undefined);
    assert.equal(chapterStep.overlay_mode, "full-scrim");
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
  assert.deepEqual(
    basics.chapters.find(current => current.id === "settings").step_ids,
    [
      "settings-introduction",
      "lithophane-stack",
      "open-settings",
      "settings-profile",
      "settings-solve-resolution",
      "settings-layer-construction",
      "close-settings",
      "solve-first",
    ],
  );
  assert.equal(
    app.commands.getGuideStep(basics, "settings-introduction").body,
    "**Settings** controls how Prisma turns your image and the filaments in your active palette into a printable layer plan. This process is called solving an image.\n\nPrisma exposes many parameters so you can customize this process, but you only need to understand a small set of them to use Prisma effectively.",
  );
  const lithophaneStack = app.commands.getGuideStep(basics, "lithophane-stack");
  assert.equal(lithophaneStack.companion.layout, "single");
  assert.equal(
    lithophaneStack.companion.items[0].src,
    "/assets/guides/settings-drawer/lithophane-stack-placeholder-v1.svg",
  );
  assert.equal(app.commands.getGuideStep(basics, "settings-profile").target_id, "settings.drawer-overview");
  assert.equal(
    app.commands.getGuideStep(basics, "settings-solve-resolution").target_id,
    "settings.essentials-resolution",
  );
  assert.equal(
    app.commands.getGuideStep(basics, "settings-layer-construction").target_id,
    "settings.essentials-construction",
  );
  assert.match(
    app.commands.getGuideStep(basics, "settings-solve-resolution").body,
    /Leave \*\*Solve Mode\*\* set to \*\*Color\*\*/,
  );
  assert.doesNotMatch(
    app.commands.getGuideStep(basics, "settings-solve-resolution").body,
    /Luminance/,
  );
  assert.match(
    app.commands.getGuideStep(basics, "settings-layer-construction").body,
    /must exactly match the layer height used by your slicer/,
  );
  assert.deepEqual(
    basics.chapters.find(current => current.id === "image").step_ids.slice(0, 5),
    [
      "image-introduction",
      "image-library",
      "image-library-maintenance",
      "choose-image",
      "image-preview",
    ],
  );
  const imageIntroduction = app.commands.getGuideStep(basics, "image-introduction");
  assert.equal(imageIntroduction.reveal_id, "workflow.image-page");
  assert.equal(imageIntroduction.target_id, "workflow.image");
  assert.equal(imageIntroduction.overlay_mode, "spotlight");
  assert.match(imageIntroduction.body, /define what you want your lithophane to look like and how big it will be/);
  assert.match(imageIntroduction.body, /resulting image as the visual target/);
  const imageLibrary = app.commands.getGuideStep(basics, "image-library");
  assert.equal(imageLibrary.title, "Adding images to the Image Library");
  assert.match(imageLibrary.body, /Image Library contains the images available for use in Prisma/);
  assert.match(imageLibrary.body, /contents reflect the image files stored in Prisma’s Images folder/);
  assert.match(imageLibrary.body, /Use \*\*Add Image\*\* to import one or more images/);
  assert.equal(imageLibrary.note.label, "Images folder");
  assert.equal(imageLibrary.note.text, "{{imagesFolder}}");
  assert.doesNotMatch(imageLibrary.body, /\{\{imagesFolder\}\}/);
  const imageLibraryMaintenance = app.commands.getGuideStep(
    basics,
    "image-library-maintenance",
  );
  assert.match(imageLibraryMaintenance.body, /updates each time you start Prisma/);
  assert.match(imageLibraryMaintenance.body, /\*\*Expand\*\* button/);
  const chooseImage = app.commands.getGuideStep(basics, "choose-image");
  assert.match(chooseImage.body, /\*\*\{\{tutorialImage\}\}\*\*/);
  assert.equal(chooseImage.completion.accept_preexisting, true);
  assert.equal(
    basics.steps.findIndex(step => step.id === "palette-introduction") + 1,
    basics.steps.findIndex(step => step.id === "palette-methods"),
  );
  const paletteIntroduction = app.commands.getGuideStep(basics, "palette-introduction");
  assert.equal(paletteIntroduction.title, "Palette: choose your filaments");
  assert.match(
    paletteIntroduction.body,
    /Recall that a palette is the set of filaments Prisma will use to recreate the colors in an image/,
  );
  assert.match(paletteIntroduction.body, /create a palette yourself/);
  assert.doesNotMatch(paletteIntroduction.body, /active palette|next solve/i);
  assert.doesNotMatch(paletteIntroduction.body, /\bordered\b|above the white base/i);
  const paletteMethods = app.commands.getGuideStep(basics, "palette-methods");
  assert.match(paletteMethods.body, /Auto-Suggest recommends palettes/);
  assert.match(paletteMethods.body, /only include filaments that are selected in this panel/);
  assert.match(paletteMethods.body, /Deselecting a filament prevents Auto-Suggest/);
  assert.match(paletteMethods.body, /white cap and base/);
  assert.match(paletteMethods.body, /never be included in palette suggestions/);
  assert.match(paletteMethods.body, /selection is made in \*\*Settings\*\*/);
  const autosuggestControls = app.commands.getGuideStep(basics, "autosuggest-controls");
  assert.equal(autosuggestControls.target_id, "palette.autosuggest-controls");
  assert.match(autosuggestControls.body, /\*\*Palette Colors\*\*/);
  assert.match(autosuggestControls.body, /\*\*Solve Mode\*\*/);
  assert.match(autosuggestControls.body, /\*\*Suggestions\*\*/);
  assert.match(autosuggestControls.body, /Color or Luminance solve modes/);
  assert.match(autosuggestControls.body, /\[PLACEHOLDER FOR NAME OF GUIDE WHEN I WRITE IT\]/);
  assert.doesNotMatch(autosuggestControls.body, /Max Colors|Extra Color Loads/i);
  const suggestPalettes = app.commands.getGuideStep(basics, "suggest-palettes");
  assert.match(suggestPalettes.body, /\*\*Palette Colors\*\* is set to exactly 3/);
  assert.match(suggestPalettes.body, /\*\*Suggestions\*\* is at least 2/);
  assert.match(suggestPalettes.body, /Select \*\*Suggest Palettes\*\* to continue/);
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
  assert.equal(imagePreviewStep.target_id, "image.preview-adjustments");
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
  assert.match(
    app.commands.getGuideStep(basics, "physical-dimensions").body,
    /divisible by the Solve Pitch\.$/,
  );
  assert.doesNotMatch(
    app.commands.getGuideStep(basics, "physical-dimensions").body,
    /learn more about what this means/i,
  );
  assert.equal(app.commands.getGuideStep(basics, "border").target_id, "image.border");
  const imageSummary = app.commands.getGuideStep(basics, "image-summary");
  assert.match(imageSummary.body, /Solve Pitch \(the physical spacing between pixels/);
  assert.doesNotMatch(imageSummary.body, /usually requires more time/);
  const tutorialCanvas = basics.steps.find(step => step.id === "tutorial-canvas");
  assert.match(tutorialCanvas.body, /size is set to 90 × 120 mm/);
  assert.doesNotMatch(tutorialCanvas.body, /Aspect Ratio|border is off/);
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
    "image.library-import",
  );
  assert.equal(
    app.commands.getGuideStep(basics, "image-library-maintenance").target_id,
    "image.library-maintenance",
  );
  assert.doesNotMatch(
    app.commands.getGuideStep(basics, "image-library").body,
    /prepares imported images|modifying their originals/i,
  );
  const resetImageControls = basics.steps.find(step => step.id === "reset-image-controls");
  assert.equal(resetImageControls.target_id, "image.reset-framing");
  assert.equal(resetImageControls.completion.kind, "event");
  assert.equal(resetImageControls.completion.event, "image.controls-reset");
  assert.equal(resetImageControls.completion.predicate_id, "basics.image-reset");
  assert.equal(resetImageControls.completion.accept_preexisting, false);
  assert.match(resetImageControls.body, /Reset does not change whether a border is enabled/);
  assert.equal(basics.steps.find(step => step.id === "tutorial-canvas").target_id, "image.tutorial-canvas");
  assert.equal(app.commands.getGuideStep(basics, "manual-palette-add").target_id, "palette.manual");
  assert.equal(app.commands.getGuideStep(basics, "manual-palette-remove").target_id, "basics.manual-card");
  assert.equal(app.commands.getGuideStep(basics, "open-palette-variant").target_id, "basics.palette-a");
  assert.equal(app.commands.getGuideStep(basics, "add-palette-variant").target_id, "palette.manual");
  assert.equal(app.commands.getGuideStep(basics, "remove-palette-variant").target_id, "basics.variant-card");
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-extrusion-width").completion.kind,
    "event",
  );
  assert.equal(
    basics.steps.find(step => step.id === "tutorial-extrusion-width").completion.predicate_id,
    "basics.tutorial-extrusion-width-active",
  );
  assert.match(
    basics.steps.find(step => step.id === "tutorial-extrusion-width").body,
    /0\.40 mm/,
  );
  for (const stepId of [
    "printer-select",
    "choose-image",
    "reset-image-controls",
    "open-palette",
    "suggest-palettes",
    "add-suggestions",
    "manual-palette-add",
    "manual-palette-remove",
    "open-palette-variant",
    "add-palette-variant",
    "remove-palette-variant",
    "activate-first",
    "open-settings",
    "advanced-settings",
    "close-settings",
    "solve-first",
    "activate-second",
    "solve-second",
    "open-export",
    "select-export-run",
    "generate-files",
  ]) {
    assert.equal(
      app.commands.getGuideStep(basics, stepId).completion.kind,
      "event",
      `${stepId} should advance after its requested action succeeds`,
    );
  }
  const terminologyStep = basics.steps.find(step => step.id === "core-terminology");
  assert.match(terminologyStep.body, /A \*\*palette\*\* is/);
  assert.match(terminologyStep.body, /A \*\*solve\*\* is/);
  assert.doesNotMatch(terminologyStep.body, /[“"](?:palette|solve)[”"]/i);
  assert.doesNotMatch(terminologyStep.body, /many possible ways/);
  const paletteTools = app.commands.getGuideDetour(basics, "palette-tools");
  assert.deepEqual(
    paletteTools.steps.map(step => step.id),
    [
      "manual-palette-add",
      "manual-palette-remove",
      "open-palette-variant",
      "add-palette-variant",
      "remove-palette-variant",
      "palette-saving-pointer",
    ],
  );
  const settingsTools = app.commands.getGuideDetour(basics, "settings-tools");
  assert.deepEqual(
    settingsTools.steps.map(step => step.id),
    ["settings-profiles", "preprocessing-solver", "white-cap", "advanced-settings"],
  );
  app.state.guides.currentGuide = basics;
  app.state.guides.runtimeContext = { imagesFolder: "C:\\PrismaRuntime\\Images" };
  assert.equal(
    app.commands.formatGuideText(app.commands.getGuideStep(basics, "image-library").note.text).includes(
      "C:\\PrismaRuntime\\Images",
    ),
    true,
  );
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
  const paletteGuide = app.commands.getGuideDefinition("palette-guide");
  const settingsGuide = app.commands.getGuideDefinition("settings-guide");
  const previewGuide = app.commands.getGuideDefinition("preview-guide");
  const exportGuide = app.commands.getGuideDefinition("export-guide");
  assert.deepEqual(
    [imageGuide.version, paletteGuide.version, settingsGuide.version, previewGuide.version, exportGuide.version],
    [6, 7, 7, 7, 7],
  );
  assert.equal(
    app.commands.getGuideStep(imageGuide, "aspect-ratios"),
    app.commands.getGuideStep(basics, "aspect-ratios"),
  );
  assert.equal(
    app.commands.getGuideStep(settingsGuide, "white-cap"),
    app.commands.getGuideStep(basics, "white-cap"),
  );
  assert.equal(settingsGuide.baseline.ghost_printer, false);
  assert.deepEqual(settingsGuide.baseline.guide_assets, []);
  assert.equal(app.commands.getCatalogGuides().length, 10);
});

test("Saving & Loading uses the approved route and default-deny durable identities", async () => {
  const { app } = await createFeatureHarness();
  const guide = app.commands.getGuideDefinition("saving-and-loading");
  const expectedIds = [
    "saving-loading-introduction", "prepare-saving-loading-workspace",
    "palette-saving-introduction", "save-palette", "remove-saved-palette-from-deck",
    "load-saved-palette", "delete-saved-palette-record", "settings-saving-introduction",
    "modify-basic-profile", "save-named-settings-profile", "modify-named-settings-profile",
    "open-settings-profiles-with-modified-draft", "load-basic-discard-settings-draft",
    "delete-named-settings-profile", "solved-run-saving-introduction",
    "solve-for-saving-loading", "save-solved-run", "clear-solve-history-for-load",
    "clear-palette-deck-for-run-load", "load-complete-saved-run", "open-run-settings",
    "use-run-settings", "explain-temp-run-profile", "delete-saved-run-record",
    "export-saving-introduction", "explain-export-file-ownership", "explain-export-downloads",
    "persistence-boundaries-introduction", "unsaved-working-state-boundaries",
    "saving-loading-complete",
  ];
  assert.deepEqual(guide.steps.map(step => step.id), expectedIds);
  assert.equal(guide.chapters.length, 6);
  assert.equal(guide.catalog.group, "Save, Reuse, and Export");
  assert.equal(guide.steps[0].next_label, "Begin");
  assert.equal(guide.steps[0].allow_previous, false);
  assert.equal(
    app.commands.getGuideStep(guide, "save-palette").viewport_anchor,
    "center-right",
  );
  assert.equal(
    app.commands.getGuideStep(guide, "remove-saved-palette-from-deck").viewport_anchor,
    "center-right",
  );
  assert.equal(
    app.commands.getGuideStep(guide, "save-named-settings-profile").viewport_anchor,
    "center-left",
  );
  assert.equal(
    app.commands.getGuideStep(guide, "solve-for-saving-loading").body.includes("Palette Palette"),
    false,
  );

  app.state.guides.currentGuide = guide;
  app.state.guides.currentStep = app.commands.getGuideStep(guide, "save-palette");
  app.state.guides.runtimeState = "presenting";
  app.state.guides.runtimeContext = {
    tutorialPalettes: [{ id: "primary-card", filament_ids: ["cyan", "magenta", "yellow"] }],
    names: { savedPaletteName: "Saving & Loading Palette" },
  };
  assert.equal(app.commands.authorizeGuideDurableMutation("palette.saved.create", {
    deck_card_id: "primary-card",
    palette_signature: ["cyan", "magenta", "yellow"],
    name: "Saving & Loading Palette",
  }), true);
  assert.equal(app.commands.authorizeGuideDurableMutation("palette.saved.create", {
    deck_card_id: "unrelated-card",
    palette_signature: ["cyan", "magenta", "yellow"],
    name: "Saving & Loading Palette",
  }), false);
  assert.equal(app.commands.authorizeGuideDurableMutation("settings.profile.set-startup", {
    profile_id: "any-profile",
  }), false);
});

test("Settings Drawer defines the complete repeatable hub-and-spoke route", async () => {
  const { app } = await createFeatureHarness();
  const guide = app.commands.getGuideDefinition("settings-drawer");
  const steps = app.commands.getAllGuideSteps(guide);
  assert.equal(steps.length, 59);
  assert.equal(new Set(steps.map(step => step.id)).size, 59);
  assert.ok(steps.every(step => step.id.startsWith("settings-drawer.")));
  assert.deepEqual(guide.chapters[0].step_ids, guide.steps.map(step => step.id));
  assert.equal(guide.detours.length, 5);
  assert.deepEqual(guide.steps.map(step => step.id), [
    "settings-drawer.intro",
    "settings-drawer.enable-advanced",
    "settings-drawer.chapters",
  ]);
  const setup = guide.steps[1];
  assert.equal(setup.target_id, "settings-drawer.open-and-advanced");
  assert.equal(setup.viewport_anchor, "center");
  assert.equal(setup.completion.kind, "event");
  assert.deepEqual(setup.completion.events, ["settings.opened", "settings.advanced-changed"]);
  assert.equal(setup.completion.predicate_id, "settings.drawer-open-and-advanced");
  assert.equal(setup.completion.auto_advance, true);
  assert.deepEqual(setup.complete_actions[0].input.locks, [
    "settings-drawer-open",
    "settings-advanced-on",
  ]);
  const hub = guide.steps.at(-1);
  assert.equal(hub.allow_end, false);
  assert.equal(hub.detour_layout, "button-description");
  assert.equal(hub.card_size, "wide");
  const detourStepIds = (id) => guide.detours.find((current) => current.id === id)
    .steps.map((current) => current.id.replace(`${id}.`, ""));
  assert.deepEqual(detourStepIds("settings-drawer.essentials"), [
    "intro", "stack", "solve-mode", "solve-mode-choice", "solve-pitch",
    "solve-pitch-matching", "layer-height", "layer-height-tradeoff",
    "max-total-thickness", "thickness-budget", "base-thickness",
    "min-cap-layers", "white-filament",
  ]);
  assert.deepEqual(detourStepIds("settings-drawer.white-cap"), [
    "intro", "cap-style", "appearance-budget", "smoothing-radius", "detail-depth",
  ]);
  assert.deepEqual(detourStepIds("settings-drawer.luminance"), [
    "intro", "what-changes", "drawer-changes", "max-total-thickness",
    "base-thickness", "min-cap-layers", "white-filament", "preprocessing",
    "appearance-model", "white-point-rescale", "chroma-weight", "region-controls",
    "shading-balance", "shading-balance-suggest", "smoothing-radius", "detail-depth",
  ]);
  const stack = steps.find(step => step.id === "settings-drawer.essentials.stack");
  assert.equal(stack.target_id, null);
  assert.equal(stack.companion.layout, "single");
  assert.equal(
    stack.companion.items[0].src,
    "/assets/guides/settings-drawer/lithophane-stack-placeholder-v1.svg",
  );
  for (const current of guide.detours) {
    assert.equal(current.offer_step_id, "settings-drawer.chapters");
    assert.equal(current.return_step_id, "settings-drawer.chapters");
    assert.equal(current.return_predicate_id, null);
    assert.equal(current.repeatable, true);
    assert.equal(current.previous_returns_to_offer, true);
    assert.equal(current.suppress_previous_on_return, true);
    assert.equal(current.exit_label, "Back to chapters");
    assert.equal(current.allow_exit_on_final, true);
    assert.equal(current.button_label, current.label);
    assert.equal(current.show_status, false);
  }
  app.state.guides.currentGuide = guide;
  app.state.guides.currentStep = guide.steps.at(-1);
  app.state.guides.completedDetourIds = new Set(guide.detours.map(item => item.id));
  app.state.guides.activeDetour = null;
  assert.deepEqual(
    app.commands.getOfferedGuideDetours().map(item => item.id),
    guide.detours.map(item => item.id),
  );
});

test("Settings Drawer chapter navigation returns cleanly from every route and remains repeatable", async () => {
  const { app } = await createFeatureHarness();
  const guide = app.commands.getGuideDefinition("settings-drawer");
  const hub = guide.steps.at(-1);
  const actionCalls = [];
  app.commands.executeGuideAction = async descriptor => actionCalls.push(descriptor);
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});
  app.commands.restoreGuidePresentation = () => {};

  const resetAtHub = () => {
    app.state.guides.currentGuide = guide;
    app.state.guides.currentStep = hub;
    app.state.guides.runtimeState = "presenting";
    app.state.guides.completedStepIds = new Set();
    app.state.guides.completedDetourIds = new Set();
    app.state.guides.visitedDetourIds = new Set();
    app.state.guides.actionLedger = new Set();
    app.state.guides.activeDetour = null;
    app.state.guides.reviewingCompletedStep = false;
    app.state.guides.suppressPreviousForCurrentArrival = false;
  };

  resetAtHub();
  assert.equal(app.commands.startGuideDetour("settings-drawer.luminance"), true);
  await settleGuideStepActions(app);
  assert.equal(app.state.guides.currentStep.id, "settings-drawer.luminance.intro");
  assert.ok(actionCalls.some(call => call.input?.values?.luminance_mode === "luminance_detail"));
  assert.equal(app.commands.previousGuideStep(), true);
  await settleGuideStepActions(app);
  assert.equal(app.state.guides.currentStep, hub);
  assert.equal(app.state.guides.suppressPreviousForCurrentArrival, true);
  assert.equal(app.commands.previousGuideStep(), false);

  for (const route of guide.detours) {
    for (const index of [0, Math.floor(route.steps.length / 2), route.steps.length - 1]) {
      resetAtHub();
      assert.equal(app.commands.startGuideDetour(route.id), true);
      await settleGuideStepActions(app);
      app.state.guides.currentStep = route.steps[index];
      app.state.guides.runtimeState = "presenting";
      assert.equal(app.commands.exitGuideDetour(), true, `${route.id} index ${index}`);
      await settleGuideStepActions(app);
      assert.equal(app.state.guides.currentStep, hub);
      assert.equal(app.state.guides.visitedDetourIds.has(route.id), true);
    }

    resetAtHub();
    assert.equal(app.commands.startGuideDetour(route.id), true);
    await settleGuideStepActions(app);
    app.state.guides.currentStep = route.steps.at(-1);
    app.state.guides.runtimeState = "presenting";
    assert.equal(app.commands.nextGuideStep(), true);
    await settleGuideStepActions(app);
    assert.equal(app.state.guides.currentStep, hub);
    assert.equal(app.state.guides.completedDetourIds.has(route.id), true);
    assert.ok(app.commands.getOfferedGuideDetours().some(item => item.id === route.id));
    assert.equal(app.commands.startGuideDetour(route.id), true);
    await settleGuideStepActions(app);
    assert.equal(app.state.guides.currentStep, route.steps[0]);
  }
});

test("Guide durable deletion reuses the exact identity journaled at creation", async () => {
  const { app } = await createFeatureHarness();
  const guide = app.commands.getGuideDefinition("saving-and-loading");
  const transitions = [];
  app.state.guides.currentGuide = guide;
  app.state.guides.workspaceSessionId = "guide-session";
  app.state.guides.runtimeContext = {};
  app.api.transitionGuideResource = async (_sessionId, resource) => {
    transitions.push(clone(resource));
    return resource;
  };
  app.api.getRequestContext = () => ({});
  app.api.setRequestContext = () => {};

  await app.commands.performGuideDurableMutation({
    direction: "create",
    operationId: "saving-loading-profile",
    kind: "settings-profile",
    name: "Saving & Loading Profile",
    fingerprint: { settings: { t_max: 2.6 }, modules: { color: true } },
    resolveId: response => response.id,
  }, async () => ({ id: "profile-id" }));
  await app.commands.performGuideDurableMutation({
    direction: "delete",
    operationId: "saving-loading-profile",
    kind: "settings-profile",
    id: "profile-id",
    name: "Saving & Loading Profile",
    fingerprint: { settings: { t_max: 2.6000000001 }, modules: { color: true } },
  }, async () => ({ deleted: "profile-id" }));

  assert.deepEqual(transitions.map(item => item.status), [
    "pending_create", "present", "pending_delete", "absent",
  ]);
  assert.deepEqual(transitions[2].fingerprint, transitions[0].fingerprint);
  assert.equal(app.state.guides.runtimeContext.durableResources["saving-loading-profile"], undefined);
});

test("Basics accepts an already-active Tutorial Printer or a successful later selection", async () => {
  async function startWithPrinter(activePrinterId) {
    const profile = tutorialPrinterProfile();
    const printerConfigPage = fakeElement();
    printerConfigPage.classList.add("is-hidden");
    const { app } = await createFeatureHarness({
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
          nozzle_profiles: [{ id: "bambu-nozzle-200", diameter_um: 200, min_layer_height_um: 50, max_layer_height_um: 150, max_extrusion_width_um: 250, minimum_line_length_multiplier: 2 }],
        },
        clone(profile),
      ],
      active_printer_id: activePrinterId,
      revision: 1,
      printer_setup_state: {
        "bambu-x1c": { active_nozzle_id: "bambu-nozzle-200", nozzle_width_state: { "bambu-nozzle-200": { current_width_um: 200, saved_widths_um: [200] } } },
        "tutorial-printer": { active_nozzle_id: "nozzle-400", nozzle_width_state: { "nozzle-200": { current_width_um: 200, saved_widths_um: [200] }, "nozzle-400": { current_width_um: 400, saved_widths_um: [400] } } },
      },
    };
    app.commands.loadPrinters = async () => {};
    app.commands.selectActivePrintSetup = async ({ active_printer_id: printerId, active_nozzle_id: nozzleId, current_width_um: widthUm }) => {
      const data = app.state.session.printersData;
      if (printerId) data.active_printer_id = printerId;
      const active = data.printers.find(item => item.id === data.active_printer_id);
      const setup = data.printer_setup_state[active.id];
      if (nozzleId) setup.active_nozzle_id = nozzleId;
      if (widthUm) setup.nozzle_width_state[setup.active_nozzle_id].current_width_um = widthUm;
      const nozzle = active.nozzle_profiles.find(item => item.id === setup.active_nozzle_id);
      return { printer: active, nozzle, extrusion_width: { width_um: setup.nozzle_width_state[setup.active_nozzle_id].current_width_um } };
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
    assert.equal(await app.commands.startGuide("prisma-generator-basics"), true);
    return app;
  }

  const alreadyActive = await startWithPrinter("tutorial-printer");
  assert.equal(alreadyActive.state.guides.currentStep.id, "introduction");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "core-terminology");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "lithophane-image-principle");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "prisma-creation-overview");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "workflow");
  alreadyActive.commands.nextGuideStep();
  assert.equal(alreadyActive.state.guides.currentStep.id, "tutorial-extrusion-width");
  await settleGuideStepActions(alreadyActive);
  assert.equal(alreadyActive.state.guides.runtimeState, "presenting");
  const alreadyActiveSetup = alreadyActive.state.session.printersData.printer_setup_state["tutorial-printer"];
  assert.equal(alreadyActiveSetup.nozzle_width_state["nozzle-200"].current_width_um, 200);
  alreadyActiveSetup.active_nozzle_id = "nozzle-400";
  alreadyActive.events.emit("printer.extrusion-width-changed", { extrusionWidthUm: 400 });
  assert.equal(alreadyActive.state.guides.currentStep.id, "image-introduction");

  const selectedLater = await startWithPrinter("bambu-x1c");
  assert.equal(selectedLater.state.guides.currentStep.id, "introduction");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "core-terminology");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "lithophane-image-principle");
  selectedLater.commands.nextGuideStep();
  assert.equal(selectedLater.state.guides.currentStep.id, "prisma-creation-overview");
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
  assert.equal(selectedLater.state.guides.currentStep.id, "tutorial-extrusion-width");
  await settleGuideStepActions(selectedLater);
  assert.equal(selectedLater.state.guides.runtimeState, "presenting");
  const selectedSetup = selectedLater.state.session.printersData.printer_setup_state["tutorial-printer"];
  assert.equal(selectedSetup.nozzle_width_state["nozzle-200"].current_width_um, 200);
  selectedSetup.active_nozzle_id = "nozzle-400";
  selectedLater.events.emit("printer.extrusion-width-changed", { extrusionWidthUm: 400 });
  assert.equal(selectedLater.state.guides.currentStep.id, "image-introduction");
});

test("Basics authorizes only the exact print-setup selection requested by its current step", async () => {
  const { app } = await createFeatureHarness();
  app.state.session.printersData = { active_printer_id: "tutorial-printer" };
  app.state.guides.currentStep = {
    completion: { predicate_id: "basics.tutorial-extrusion-width-active" },
  };

  assert.equal(app.commands.guideAuthorizesPrintSetupIntent({
    intent_kind: "select_nozzle",
    active_nozzle_id: "nozzle-400",
  }), true);
  assert.equal(app.commands.guideAuthorizesPrintSetupIntent({
    intent_kind: "select_extrusion_width",
    active_nozzle_id: "nozzle-400",
    current_width_um: 400,
  }), true);
  assert.equal(app.commands.guideAuthorizesPrintSetupIntent({
    intent_kind: "select_extrusion_width",
    active_nozzle_id: "nozzle-400",
    current_width_um: 450,
  }), false);
});

test("Basics mounts a protected image, resets project work, and captures only fresh top suggestions", async () => {
  const targetFilamentCount = fakeElement();
  const targetSuggestCount = fakeElement();
  targetFilamentCount.value = "7";
  targetSuggestCount.value = "1";
  const { app } = await createFeatureHarness({
    elements: {
      "#targetFilamentCount": targetFilamentCount,
      "#targetSuggestCount": targetSuggestCount,
    },
    filaments: [
      { filament_id: "eligible-a", has_profile: true },
      { filament_id: "eligible-b", has_profile: true },
      { filament_id: "excluded", has_profile: true, exclude_from_model: true },
    ],
  });
  app.state.palette.candidateSelection = new Set(["eligible-a", "removed-before-restore"]);
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
  app.commands.renderCreationTab = () => {};
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
  app.commands.appConfirm = async () => true;
  const preparationErrors = [];
  app.commands.showToast = message => preparationErrors.push(message);

  assert.equal(await app.commands.startGuide("prisma-generator-basics"), true, preparationErrors.join("\n"));
  assert.equal(app.state.guides.runtimeContext.imagesFolder, "C:\\PrismaRuntime\\Images");
  assert.equal(
    app.state.guides.runtimeContext.tutorialImageFilename,
    "Prisma Tutorial - Bubba Blanket.jpg",
  );
  assert.equal(
    app.state.guides.runtimeContext.tutorialImageSourceRef,
    "guide-image:bubba-blanket",
  );
  assert.deepEqual(
    app.state.image.availableImages.map(image => image.filename),
    ["My selected image.jpg"],
  );
  assert.deepEqual(
    app.state.image.guideImages.map(image => image.source_ref),
    ["guide-image:bubba-blanket"],
  );
  assert.equal(app.state.image.selectedImage, null);
  assert.equal(targetFilamentCount.value, "3");
  assert.equal(targetSuggestCount.value, "5");
  assert.deepEqual(
    [...app.state.palette.candidateSelection].sort(),
    ["eligible-a", "eligible-b"],
  );
  assert.equal(app.state.settings.loadedProfileRef.id, "temporary-guide-prisma-generator-basics");
  assert.equal(app.state.settings.config.solve_pitch_extrusion_width_multiplier, 1);
  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-profile-ready"), true);
  app.state.settings.config.solve_pitch_extrusion_width_multiplier = 2;
  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-profile-ready"), false);
  app.state.settings.config.solve_pitch_extrusion_width_multiplier = 1;

  const warnings = [];
  app.commands.showToast = message => warnings.push(message);
  app.state.palette.stagingDeck = [
    { id: "old", name: "Old result", filament_ids: ["x", "y", "z"] },
    { id: "new-a", name: "Fresh A", filament_ids: ["a", "b", "c"] },
    { id: "new-b", name: "Fresh B", filament_ids: ["d", "e", "f"] },
  ];
  const suggestionStep = app.commands
    .getGuideDefinition("prisma-generator-basics")
    .steps.find(step => step.id === "suggest-palettes");
  targetFilamentCount.value = "2";
  app.commands.captureGuideCompletion(suggestionStep, {
    cardIds: ["new-a", "new-b"],
  });
  assert.equal(app.state.guides.runtimeContext.paletteA, null);
  assert.match(warnings.at(-1), /Palette Colors to exactly 3/);

  targetFilamentCount.value = "3";
  targetSuggestCount.value = "1";
  app.commands.captureGuideCompletion(suggestionStep, {
    cardIds: ["new-a", "new-b"],
  });
  assert.equal(app.state.guides.runtimeContext.paletteA, null);
  assert.match(warnings.at(-1), /Suggestions to at least 2/);

  targetSuggestCount.value = "5";
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
  assert.equal(app.commands.guidePredicateSatisfied("basics.two-suggestions-ready"), true);

  await app.commands.endGuide();
  assert.deepEqual([...app.state.palette.candidateSelection], ["eligible-a"]);
  assert.equal(targetFilamentCount.value, "7");
  assert.equal(targetSuggestCount.value, "1");
  assert.equal(app.state.image.guideImages.length, 0);
  assert.equal(app.state.image.selectedImage, null);
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

test("Settings Guide uses the teaching workspace without mounting printer or image assets", async () => {
  const targetFilamentCount = fakeElement();
  targetFilamentCount.value = "8";
  const { app } = await createFeatureHarness({
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
  app.commands.renderImageTab = () => {};
  app.commands.renderCreationTab = () => {};
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
  app.commands.appConfirm = async () => true;
  const preparationErrors = [];
  app.commands.showToast = message => preparationErrors.push(message);

  assert.equal(await app.commands.startGuide("settings-guide"), true, preparationErrors.join("\n"));
  assert.equal(app.state.image.selectedImage, null);
  assert.equal(app.state.image.guideImages.length, 0);
  assert.deepEqual([...app.state.palette.candidateSelection], []);
  assert.equal(app.state.palette.candidateInitialized, true);
  assert.equal(targetFilamentCount.value, "3");
  assert.equal(app.state.settings.loadedProfileRef.id, "temporary-guide-settings-guide");
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

  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-extrusion-width-active"), true);
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
  app.state.session.printersData.printer_setup_state["tutorial-printer"].active_nozzle_id = "nozzle-200";
  assert.equal(app.commands.guidePredicateSatisfied("basics.tutorial-extrusion-width-active"), false);
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);
  app.state.session.printersData.printer_setup_state["tutorial-printer"].active_nozzle_id = "nozzle-400";
  app.state.image.frameState.widthMm = 90.5;
  assert.equal(app.commands.guidePredicateSatisfied("basics.canvas-ready"), false);

  Object.assign(app.state.image.frameState, {
    arMode: "image",
    widthMm: 120,
    heightMm: 160,
  });
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), true);
  app.state.settings.config.border = true;
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), false);
  app.state.settings.config.border = false;
  app.state.image.frameState.rotation = 5;
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), false);
  app.state.image.frameState.rotation = 0;
  app.state.image.imageAdjust.temperature = 0.2;
  assert.equal(app.commands.guidePredicateSatisfied("basics.image-reset"), false);
});

test("Reset image controls advances only after the reset event leaves verified defaults", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const rendered = [];
  app.commands.renderGuideStep = payload => rendered.push(payload.step.id);
  app.commands.revealGuideTarget = () => {};
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = app.commands.getGuideStep(basics, "image-preview");
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;
  app.state.guides.runtimeContext = {
    tutorialImageFilename: "Prisma Tutorial - Bubba Blanket.jpg",
  };
  app.state.image.selectedImage = {
    filename: "Prisma Tutorial - Bubba Blanket.jpg",
  };
  Object.assign(app.state.image.frameState, {
    arMode: "image",
    widthMm: 120,
    heightMm: 160,
    scale: 110,
    rotation: 0,
    panX: 0,
    panY: 0,
    flipH: false,
    flipV: false,
  });
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
  app.state.settings.config.border = true;

  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "reset-image-controls");
  app.events.emit("image.controls-reset", { source: "test-dirty" });
  assert.equal(app.state.guides.currentStep.id, "reset-image-controls");

  app.state.image.frameState.scale = 100;
  app.events.emit("image.controls-reset", { source: "test-border-on" });
  assert.equal(app.state.guides.currentStep.id, "reset-image-controls");

  app.state.settings.config.border = false;
  app.events.emit("image.controls-reset", { source: "test-reset" });
  assert.equal(app.state.guides.currentStep.id, "tutorial-canvas");
  assert.deepEqual(rendered, ["reset-image-controls", "tutorial-canvas"]);
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
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;
  app.state.ui.currentTab = "image";

  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "open-palette");
  assert.equal(app.state.guides.runtimeState, "presenting");
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
  app.state.session.printersData.printer_setup_state["tutorial-printer"].active_nozzle_id = "nozzle-200";
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
  assert.equal(app.state.guides.runtimeState, "presenting");
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
  assert.equal(app.state.guides.runtimeState, "presenting");
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "white-point-rescale");
  await app.commands.endGuide();
  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(app.state.guides.currentGuide, null);
  assert.equal(putCalls, 0);
  assert.deepEqual(remote, {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  });
});

test("Basics detours return to their offering step on completion and early exit", async () => {
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
  const detourPresentation = { currentTab: "export", marker: "detour-origin" };
  const restored = [];
  app.commands.captureGuidePresentation = () => detourPresentation;
  app.commands.restoreGuidePresentation = snapshot => restored.push(snapshot);
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = offer;
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;

  assert.equal(app.commands.getOfferedGuideDetours()[0].id, "export-choices");
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
  assert.deepEqual(restored, [detourPresentation]);
  assert.equal(app.commands.startGuideDetour("export-choices"), true);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "export-introduction");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.state.guides.activeDetour, null);
  assert.equal(app.state.guides.completedDetourIds.has("export-choices"), true);
  assert.deepEqual(app.commands.getOfferedGuideDetours(), []);
  assert.equal(app.commands.getGuideDetoursAtCurrentStep()[0]?.id, "export-choices");
  assert.deepEqual(restored, [detourPresentation, detourPresentation]);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "select-export-run");
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
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set([
    detour.offer_step_id,
    detour.steps.at(-1).id,
  ]);
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = { id: detour.id };

  assert.equal(app.commands.nextGuideStep(), false);
  assert.equal(app.state.guides.currentStep.id, "advanced-settings");
  assert.match(warnings.at(-1), /restore the requested tutorial state/i);
  ready = true;
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "settings-profile");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.state.guides.activeDetour, null);
});

test("detour presentation snapshots restore page-specific UI modes", async () => {
  const { app } = await createFeatureHarness();
  app.state.ui.currentTab = "creation";
  app.state.settings.settingsDrawerOpen = false;
  app.state.settings.settingsAdvancedVisible = false;
  app.state.image.frameEditorTab = "size";
  app.state.palette.creationMode = "auto";
  app.state.solve.solveView = "predicted";
  app.state.solve.solveWhiteCapView = "cap_map";
  app.state.solve.solveColorRegionsView = "color_ceiling";
  app.state.solve.solveAdvancedViewsOpen = false;
  const snapshot = app.commands.captureGuidePresentation();
  let solveRenders = 0;
  app.commands.switchFrameEditorTab = tab => { app.state.image.frameEditorTab = tab; };
  app.commands.toggleCreationMode = mode => { app.state.palette.creationMode = mode; };
  app.commands.setSolveAdvancedViewsOpen = open => {
    app.state.solve.solveAdvancedViewsOpen = open;
  };
  app.commands.renderSolveComparisonGrid = () => { solveRenders += 1; };
  app.commands.saveSettingsAdvancedVisible = () => {};
  app.commands.updateAdvancedSettingsVisibility = () => {};
  app.commands.distributeSettingsColumns = () => {};

  app.state.settings.settingsAdvancedVisible = true;
  app.state.image.frameEditorTab = "image";
  app.state.palette.creationMode = "manual";
  app.state.solve.solveView = "surface";
  app.state.solve.solveWhiteCapView = "detail_cap_map";
  app.state.solve.solveColorRegionsView = "recipe_regions";
  app.state.solve.solveAdvancedViewsOpen = true;
  app.commands.restoreGuidePresentation(snapshot);

  assert.equal(app.state.settings.settingsAdvancedVisible, false);
  assert.equal(app.state.image.frameEditorTab, "size");
  assert.equal(app.state.palette.creationMode, "auto");
  assert.equal(app.state.solve.solveView, "predicted");
  assert.equal(app.state.solve.solveWhiteCapView, "cap_map");
  assert.equal(app.state.solve.solveColorRegionsView, "color_ceiling");
  assert.equal(app.state.solve.solveAdvancedViewsOpen, false);
  assert.equal(solveRenders, 1);
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
  await app.commands.endGuide();
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

test("first launch accepts First-Time Setup and records the response before starting", async () => {
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

test("First-Time Setup navigates back through printer steps without reopening its editor", async () => {
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
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "printer-open");
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");

  printerPage.classList.add("is-hidden");
  app.events.emit("printer-config.closed", { source: "test" });
  assert.equal(app.state.guides.currentStep.id, "active-extrusion-width");

  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(reopened, 0);
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "active-extrusion-width");
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "printer-configuration");
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "printer-open");
  assert.equal(app.commands.previousGuideStep(), false);
});

test("Palette teaching activates and tracks its temporary Manual and Variant cards", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const paletteA = { id: "palette-a", name: "Palette A", filament_ids: ["red"] };
  const paletteB = { id: "palette-b", name: "Palette B", filament_ids: ["blue"] };
  const manual = { id: "manual-1", name: "Manual", filament_ids: ["red", "blue"] };
  const variant = { id: "variant-1", name: "Palette A Variant", filament_ids: ["green"] };
  app.state.guides.currentGuide = basics;
  app.state.guides.runtimeContext = {
    paletteA,
    paletteB,
    manualCardId: null,
    manualCardRemoved: false,
    variantCardId: null,
    variantCardRemoved: false,
  };
  app.state.palette.deck = [paletteA, paletteB, manual];
  app.state.palette.activeDeckId = paletteA.id;
  app.commands.renderDeckCards = () => {};
  app.commands.updateRail = () => {};
  app.commands.syncConfigToServer = () => {};

  app.commands.captureGuideCompletion(
    app.commands.getGuideStep(basics, "manual-palette-add"),
    { action: "added", card: manual },
  );
  assert.equal(app.state.guides.runtimeContext.manualCardId, manual.id);
  assert.equal(app.state.palette.activeDeckId, manual.id);

  app.state.palette.deck = [paletteA, paletteB];
  app.commands.captureGuideCompletion(
    app.commands.getGuideStep(basics, "manual-palette-remove"),
    { action: "removed", cardId: manual.id, card: manual },
  );
  assert.equal(app.commands.guidePredicateSatisfied("basics.manual-card-removed"), true);

  app.state.palette.creationMode = "manual";
  app.state.palette.manualVariantDraft = { sourceCardId: paletteA.id };
  assert.equal(app.commands.guidePredicateSatisfied("basics.palette-variant-started"), true);

  app.state.palette.deck.push(variant);
  app.state.palette.activeDeckId = variant.id;
  app.commands.captureGuideCompletion(
    app.commands.getGuideStep(basics, "add-palette-variant"),
    { action: "added", card: variant, sourceCardId: paletteA.id },
  );
  assert.equal(app.commands.guidePredicateSatisfied("basics.palette-variant-added"), true);

  app.state.palette.deck = [paletteA, paletteB];
  app.commands.captureGuideCompletion(
    app.commands.getGuideStep(basics, "remove-palette-variant"),
    { action: "removed", cardId: variant.id, card: variant },
  );
  assert.equal(app.commands.guidePredicateSatisfied("basics.palette-variant-removed"), true);
});

test("Palette Deck detour distinguishes Continue, early exit, and completed return", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  const offer = app.commands.getGuideStep(basics, "palette-deck");
  const detour = app.commands.getGuideDetour(basics, "palette-tools");
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.state.guides.currentGuide = basics;
  app.state.guides.currentStep = offer;
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.activeDetour = null;

  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "activate-first");

  app.state.guides.currentStep = offer;
  app.state.guides.completedStepIds = new Set();
  assert.equal(app.commands.startGuideDetour("palette-tools"), true);
  assert.equal(app.state.guides.currentStep.id, "manual-palette-add");
  assert.equal(app.commands.exitGuideDetour(), true);
  assert.equal(app.state.guides.currentStep.id, "palette-deck");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.commands.getOfferedGuideDetours()[0]?.id, "palette-tools");

  app.state.guides.activeDetour = { id: detour.id };
  app.state.guides.currentStep = detour.steps.at(-1);
  app.state.guides.reviewingCompletedStep = false;
  app.state.guides.completedStepIds.add(detour.steps.at(-1).id);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "palette-deck");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(app.state.guides.activeDetour, null);
  assert.equal(app.state.guides.completedDetourIds.has("palette-tools"), true);
  assert.deepEqual(app.commands.getOfferedGuideDetours(), []);
  assert.equal(app.commands.getGuideDetoursAtCurrentStep()[0]?.id, "palette-tools");
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "activate-first");
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "palette-deck");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.deepEqual(app.commands.getOfferedGuideDetours(), []);
  assert.equal(app.commands.getGuideDetoursAtCurrentStep()[0]?.id, "palette-tools");
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
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.state.guides.runtimeState, "idle");
  assert.equal(app.state.guides.currentGuide, null);
});

test("forced First-Time Setup bypasses and does not mutate saved first-launch state", async () => {
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
  assert.equal(app.state.guides.runtimeState, "presenting");
  assert.equal(app.state.guides.targetUnavailable, true);

  app.commands.handleGuideTargetAvailable();
  assert.equal(app.state.guides.runtimeState, "presenting");
  assert.equal(app.state.guides.targetUnavailable, false);
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

test("the framing lesson keeps the Adjustments spotlight while its frames rerender", async () => {
  const { app } = await createFeatureHarness();
  const adjustments = fakeElement();
  const firstCanvas = fakeElement();
  const secondCanvas = fakeElement();
  const scale = fakeElement();
  const rotation = fakeElement();
  let canvas = firstCanvas;
  app.state.ui.$ = selector => (
    selector === "#frameCanvasWrap" ? canvas
      : selector === ".framing-editor" ? adjustments
        : null
  );
  app.state.ui.$$ = selector => (
    selector === '[data-guide-target-part="image.transform-controls"]'
      ? [scale, rotation]
      : []
  );

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.framing"),
    {
      spotlightRegions: [[adjustments]],
      frameRegions: [[firstCanvas], [scale, rotation]],
      placementRegions: [[adjustments]],
    },
  );
  canvas = secondCanvas;
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.framing"),
    {
      spotlightRegions: [[adjustments]],
      frameRegions: [[secondCanvas], [scale, rotation]],
      placementRegions: [[adjustments]],
    },
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

test("Image guide spotlights group adjacent controls and separate openings from accent frames", async () => {
  const { app } = await createFeatureHarness();
  const canvas = fakeElement();
  const imageGrid = fakeElement();
  const libraryPanel = fakeElement();
  const addImage = fakeElement();
  const openFolder = fakeElement();
  const refresh = fakeElement();
  const resize = fakeElement();
  const preview = fakeElement();
  const adjustments = fakeElement();
  const aspectControls = fakeElement();
  const transformScale = fakeElement();
  const transformRotate = fakeElement();
  const cropFit = fakeElement();
  const cropWidth = fakeElement();
  const cropHeight = fakeElement();
  const widthControl = fakeElement();
  const heightControl = fakeElement();
  const borderLabel = fakeElement();
  const borderControl = fakeElement();
  const imageTab = fakeElement();
  const imageControls = fakeElement();
  const imageInfo = fakeElement();
  const reset = fakeElement();
  const borderToggle = fakeElement();
  const tutorialCard = fakeElement();
  tutorialCard.dataset.filename = "Prisma Tutorial - Bubba Blanket.jpg";
  imageGrid.querySelectorAll = selector => selector === ".image-card" ? [tutorialCard] : [];
  app.state.guides.runtimeContext = {
    tutorialImageFilename: "Prisma Tutorial - Bubba Blanket.jpg",
  };
  app.state.ui.$ = selector => ({
    "#imageGrid": imageGrid,
    "#imageLibraryPanel": libraryPanel,
    "#imagePreviewPane": preview,
    ".framing-editor": adjustments,
    "#frameControlsSize .ctrl-section-grid2": aspectControls,
    "#frameCanvasWrap": canvas,
    '[data-guide-target="image.adjustment-image-tab"]': imageTab,
    "#frameControlsImage": imageControls,
    "#imageInfoGrid": imageInfo,
    "#borderToggle": borderToggle,
  })[selector] || null;
  app.state.ui.$$ = selector => ({
    ".upload-btn, #imageLibraryOpenFolderBtn": [addImage, openFolder],
    "#imageLibraryRefreshBtn, #libraryResizeBtn": [refresh, resize],
    '[data-guide-target-part="image.transform-controls"]': [transformScale, transformRotate],
    "#fitImageBtn, #fillWidthBtn, #fillHeightBtn": [cropFit, cropWidth, cropHeight],
    '[data-guide-target-part="image.canvas-dimensions"]': [widthControl, heightControl],
    '[data-guide-target-part="image.border-controls"]': [borderLabel, borderControl],
    '[data-guide-target-part="image.canvas-reset"]': [reset],
  })[selector] || [];

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.aspect-experiment"),
    {
      spotlightRegions: [[adjustments]],
      frameRegions: [[aspectControls]],
      placementRegions: [[adjustments]],
    },
  );
  for (const [targetId, frameRegions] of [
    ["image.framing", [[canvas], [transformScale, transformRotate]]],
    ["image.crop-fit", [[cropFit, cropWidth, cropHeight]]],
    ["image.physical-dimensions", [[widthControl, heightControl]]],
    ["image.border", [[borderLabel, borderControl]]],
    ["image.adjustment-image-tab", [[imageTab]]],
    ["image.appearance", [[imageControls]]],
  ]) {
    assert.deepEqual(
      app.commands.resolveGuideTargetLayout(targetId),
      {
        spotlightRegions: [[adjustments]],
        frameRegions,
        placementRegions: [[adjustments]],
      },
      targetId,
    );
  }
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.summary"),
    {
      spotlightRegions: [[adjustments], [imageInfo]],
      frameRegions: [[imageInfo]],
      placementRegions: [[adjustments]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.reset-framing"),
    {
      spotlightRegions: [[reset], [borderToggle]],
      frameRegions: [[reset], [borderToggle]],
      placementRegions: [[adjustments]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.tutorial-canvas"),
    {
      spotlightRegions: [[widthControl, heightControl]],
      frameRegions: [[widthControl, heightControl]],
      placementRegions: [[adjustments]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.library-import"),
    [[imageGrid], [addImage, openFolder]],
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetRegions("image.library-maintenance"),
    [[imageGrid], [refresh, resize]],
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("image.preview-adjustments"),
    {
      spotlightRegions: [[preview], [adjustments]],
      frameRegions: [[preview], [adjustments]],
      placementRegions: [[adjustments]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("basics.tutorial-image"),
    {
      spotlightRegions: [[libraryPanel]],
      frameRegions: [[tutorialCard]],
      placementRegions: [[libraryPanel]],
    },
  );
  assert.equal(app.commands.resolveGuideTarget("basics.tutorial-image"), tutorialCard);
});

test("Palette Auto-Suggest controls use a broad opening and a settings-only frame", async () => {
  const { app } = await createFeatureHarness();
  const controlsPane = fakeElement();
  const paletteColors = fakeElement();
  const suggestions = fakeElement();
  const solveMode = fakeElement();
  app.state.ui.$ = selector => (
    selector === ".creation-controls-pane" ? controlsPane : null
  );
  app.state.ui.$$ = selector => (
    selector === ".creation-controls-pane .suggest-field"
      ? [paletteColors, suggestions, solveMode]
      : []
  );

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("palette.autosuggest-controls"),
    {
      spotlightRegions: [[controlsPane]],
      frameRegions: [[paletteColors, suggestions, solveMode]],
      placementRegions: [[controlsPane]],
    },
  );
});

test("the Image chapter reveal establishes its workflow page without a target", async () => {
  const { app } = await createFeatureHarness();
  const switched = [];
  app.state.ui.currentTab = "creation";
  app.commands.switchTab = tab => {
    switched.push(tab);
    app.state.ui.currentTab = tab;
  };

  app.commands.revealGuideTarget("workflow.image-page");

  assert.deepEqual(switched, ["image"]);
  assert.equal(app.state.ui.currentTab, "image");
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

test("Palette Deck spotlights its header and cards without an accent frame", async () => {
  const { app } = await createFeatureHarness();
  const deck = fakeElement();
  app.state.ui.$ = selector => selector === "#railDeck" ? deck : null;

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("palette.deck"),
    {
      spotlightRegions: [[deck]],
      frameRegions: [],
      placementRegions: [[deck]],
    },
  );
});

test("Settings steps spotlight their sections and frame the controls being discussed", async () => {
  const { app } = await createFeatureHarness();
  const drawer = fakeElement();
  const essentials = fakeElement();
  const profiles = fakeElement();
  const resolutionRows = [fakeElement(), fakeElement()];
  const constructionRows = [fakeElement(), fakeElement(), fakeElement(), fakeElement(), fakeElement()];
  app.state.ui.$ = selector => ({
    "#settingsDrawer": drawer,
    '[data-settings-group="geometry"]': essentials,
    '[data-guide-target="settings.profiles"]': profiles,
  })[selector] || null;
  app.state.ui.$$ = selector => ({
    '[data-guide-target-part="settings.essentials-resolution"]': resolutionRows,
    '[data-guide-target-part="settings.essentials-construction"]': constructionRows,
  })[selector] || [];

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("settings.drawer-overview"),
    {
      spotlightRegions: [[drawer]],
      frameRegions: [],
      placementRegions: [[drawer]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("settings.essentials-resolution"),
    {
      spotlightRegions: [[essentials]],
      frameRegions: [resolutionRows],
      placementRegions: [[essentials]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("settings.essentials-construction"),
    {
      spotlightRegions: [[essentials]],
      frameRegions: [constructionRows],
      placementRegions: [[essentials]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("settings.profiles"),
    {
      spotlightRegions: [[profiles]],
      frameRegions: [[profiles]],
      placementRegions: [[profiles]],
    },
  );
});

test("the Settings opening step turns Advanced off before the drawer opens", async () => {
  const { app } = await createFeatureHarness();
  let visibilityUpdates = 0;
  let drawerCloses = 0;
  app.state.settings.settingsDrawerOpen = true;
  app.state.settings.settingsAdvancedVisible = true;
  app.commands.closeSettingsDrawer = () => {
    drawerCloses += 1;
    app.state.settings.settingsDrawerOpen = false;
  };
  app.commands.updateAdvancedSettingsVisibility = () => { visibilityUpdates += 1; };

  app.commands.revealGuideTarget("topbar.settings");

  assert.equal(drawerCloses, 1);
  assert.equal(app.state.settings.settingsAdvancedVisible, false);
  assert.equal(visibilityUpdates, 1);
});

test("First-Time Setup separates printer spotlights from their requested accent frames", async () => {
  const { app } = await createFeatureHarness();
  const printerRail = fakeElement();
  const configureButton = fakeElement();
  const configurationWindow = fakeElement();
  const configurationFields = fakeElement();
  app.state.ui.$ = selector => ({
    '[data-guide-target="sidebar.printer"]': printerRail,
    "#printerConfigBtn": configureButton,
    '[data-guide-target="printer.configuration"]': configurationWindow,
    '[data-guide-target="printer.configuration-fields"]': configurationFields,
  })[selector] || null;

  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("sidebar.printer"),
    {
      spotlightRegions: [[printerRail]],
      frameRegions: [[configureButton]],
      placementRegions: [[printerRail]],
    },
  );
  assert.deepEqual(
    app.commands.resolveGuideTargetLayout("printer.configuration"),
    {
      spotlightRegions: [[configurationWindow]],
      frameRegions: [[configurationFields]],
      placementRegions: [[configurationWindow]],
    },
  );
});

test("guide viewport anchors remain stable for multi-surface actions", async () => {
  const { anchorGuideCardInViewport } = await import(
    moduleUrl("features/guides/overlay.js")
  );
  const viewportRect = {
    left: 0, top: 0, right: 1900, bottom: 900, width: 1900, height: 900,
  };
  const cardSize = { width: 440, height: 360 };

  assert.deepEqual(
    anchorGuideCardInViewport({ anchor: "center-right", cardSize, viewportRect }),
    { left: 1053, top: 270, placement: "viewport-center-right", docked: false },
  );
  assert.deepEqual(
    anchorGuideCardInViewport({ anchor: "center-left", cardSize, viewportRect }),
    { left: 255, top: 270, placement: "viewport-center-left", docked: false },
  );
  assert.equal(
    anchorGuideCardInViewport({
      anchor: "center-right",
      cardSize,
      viewportRect: { left: 0, top: 0, right: 1280, bottom: 720, width: 1280, height: 720 },
      avoidRects: [{ left: 445, top: 220, right: 835, bottom: 500 }],
    }),
    null,
  );
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
    path.join(appDir, "features", "guides", "registry.js"),
    "utf8",
  );
  const targets = fs.readFileSync(
    path.join(appDir, "features", "guides", "targets.js"),
    "utf8",
  );
  const overlay = fs.readFileSync(
    path.join(appDir, "features", "guides", "overlay.js"),
    "utf8",
  );
  const controller = fs.readFileSync(
    path.join(appDir, "features", "guides", "controller.js"),
    "utf8",
  );
  const eventBindings = fs.readFileSync(
    path.join(appDir, "features", "event-bindings.js"),
    "utf8",
  );
  const stackAsset = fs.readFileSync(
    path.join(appDir, "assets", "guides", "settings-drawer", "lithophane-stack-placeholder-v1.svg"),
    "utf8",
  );

  for (const target of [
    "sidebar.printer",
    "printer.configuration",
    "sidebar.active-extrusion-width",
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
  assert.match(html, /data-help-guides-action="settings-drawer"[\s\S]*?<span>Settings Drawer<\/span>/);
  assert.match(html, /data-help-guides-action="saving-and-loading"[\s\S]*?<span>Saving &amp; Loading<\/span>/);
  assert.match(controller, /action === "settings-drawer"[\s\S]*?startGuide\("settings-drawer"\)/);
  assert.match(controller, /action === "saving-and-loading"[\s\S]*?startGuide\("saving-and-loading"\)/);
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
  assert.match(overlay, /const placementTarget = unionRects\(placementTargets\);/);
  assert.match(
    overlay,
    /const avoidanceTargets = \[\.\.\.spotlightTargets, \.\.\.framedTargets\][\s\S]*?positionPairedSurfaces\(\{ targetRect: placementTarget, avoidRects: avoidanceTargets \}\)/,
  );
  assert.match(overlay, /const finalDetourStep = inDetour && isLast;[\s\S]*?const finalExitDisabled = finalDetourStep && !detour\?\.allow_exit_on_final;[\s\S]*?const stepExitDisabled = currentStep\.allow_end === false;[\s\S]*?end\.disabled = exitDisabled;/);
  assert.match(css, /#guideStepEnd:disabled\s*{[^}]*opacity:\s*0\.45;/);
  assert.match(css, /\.guide-target-frame::before\s*{[^}]*inset:\s*0;[^}]*border:\s*2px solid var\(--accent-fill\);/);
  assert.match(css, /\.guide-step-detour\[data-layout="button-description"\]\s*{[^}]*grid-template-columns:\s*max-content fit-content\(240px\);/);
  assert.match(css, /\.guide-step-detour\[data-layout="button-description"\] \.guide-step-detour-item\s*{[^}]*grid-template-columns:\s*subgrid;/);
  const resetHandler = eventBindings.match(
    /const resetBtn = app\.state\.ui\.\$\("#frameResetBtn"\);([\s\S]*?)\/\/ Generic binder for image adjustment sliders/,
  )?.[1] || "";
  assert.match(resetHandler, /syncConfigToServer\(\);\s*app\.events\.emit\("image\.controls-reset"/);
  assert.equal(
    (eventBindings.match(/app\.events\.emit\("image\.controls-reset"/g) || []).length,
    1,
  );
  assert.match(
    targets,
    /"workflow\.overview": groupedTarget\(\[[\s\S]*?selector:\s*'#solveActionSplit, \[data-guide-target="topbar\.settings"\]'[\s\S]*?all:\s*true/,
  );
  assert.match(html, /id="helpGuidesMenuBtn"[\s\S]*?aria-haspopup="menu"[\s\S]*?aria-expanded="false"[\s\S]*?aria-controls="helpGuidesMenu"/);
  assert.match(html, /id="guideStepBody"[^>]*aria-live="polite"/);
  assert.match(html, /id="guideStepNote"[^>]*aria-labelledby="guideStepNoteLabel"[^>]*hidden/);
  assert.match(html, /class="pane-title">Image Library<\/span>/);
  assert.match(html, /id="guideStepVisual"[^>]*role="img"[^>]*hidden/);
  assert.match(html, /id="guideCompanion"[^>]*role="region"[^>]*hidden/);
  assert.match(html, /id="guideCompanionHeader"[^>]*>[\s\S]*?id="guideCompanionExpand"[^>]*hidden>Enlarge<\/button>/);
  assert.match(html, /id="guideMediaLightboxClose"[^>]*class="[^"]*surface-close[^"]*"|class="[^"]*surface-close[^"]*"[^>]*id="guideMediaLightboxClose"/);
  assert.match(html, /id="guideMediaLightboxClose"[\s\S]*?<svg viewBox="0 0 12 12"/);
  assert.match(html, /id="guideMediaLightbox"[^>]*aria-hidden="true"[^>]*hidden/);
  assert.doesNotMatch(html, /data-guide-visual="lithophane-stack"/);
  assert.match(stackAsset, /<rect x="80" y="186" width="140" height="8" fill="#e8e8e8"/);
  assert.equal(
    (stackAsset.match(/<rect x="80" y="(?:146|154|162|170|178)" width="140" height="8" fill="#[0-9a-f]+"\/>/gi) || []).length,
    5,
  );
  assert.equal(
    (stackAsset.match(/<line x1="80" y1="(?:138|146|154|162|170|178|186)" x2="220" y2="(?:138|146|154|162|170|178|186)"\/>/g) || []).length,
    7,
  );
  assert.match(stackAsset, />base thickness<\/text>/);
  assert.match(stackAsset, />min cap layers<\/text>/);
  assert.match(stackAsset, />max<\/tspan>[\s\S]*?>thickness<\/tspan>/);
  assert.match(overlay, /function syncStepVisual\(\)[\s\S]*?cloneNode\(true\)[\s\S]*?syncStepVisual\(\);/);
  assert.match(overlay, /function syncStepCompanion\(\)[\s\S]*?chooseGuideSurfaceLayout\([\s\S]*?syncStepCompanion\(\);/);
  assert.match(overlay, /const expandableIndexes = model\.items[\s\S]*?headerExpand\.hidden = !hasSingleExpandableImage/);
  assert.doesNotMatch(css, /\.guide-companion__expand/);
  assert.match(css, /\.guide-companion\s*{[^}]*background:\s*var\(--drawer-bg\);/);
  assert.match(css, /\.guide-companion__content\s*{[^}]*background:\s*var\(--drawer-bg\);/);
  assert.match(overlay, /function syncStepNote\(\)[\s\S]*?formatGuideText\(note\.text\)[\s\S]*?syncStepNote\(\);/);
  assert.match(css, /\.guide-step-body\s*{[^}]*overflow-wrap:\s*anywhere;/);
  assert.match(css, /\.guide-step-body\s*{[^}]*white-space:\s*pre-line;/);
  assert.match(css, /\.guide-step-note__value\s*{[^}]*font-size:\s*10px;[^}]*overflow-wrap:\s*anywhere;/);
  assert.doesNotMatch(html, /id="guideStepCounter"/);
  assert.match(
    html,
    /class="guide-step-actions surface-footer"[\s\S]*?id="guideProgress"[\s\S]*?id="guideProgressTrack"[\s\S]*?role="progressbar"/,
  );
  assert.match(html, /id="guideStepDetour"[^>]*hidden><\/div>/);
  assert.doesNotMatch(html, /id="guideStepDetourButton"/);
  assert.match(
    overlay,
    /button\.textContent = completed && !currentDetour\.repeatable[\s\S]*?button\.disabled = completed && !currentDetour\.repeatable;/,
  );
  assert.match(css, /\.guide-step-detour-item > button:disabled\s*{[^}]*opacity:\s*0\.55;/);
  assert.match(css, /\.guide-progress\s*{[\s\S]*?border-top:/);
  assert.match(css, /\.guide-progress__segment::after\s*{[\s\S]*?width:\s*var\(--guide-progress-width, 0%\);/);
  assert.match(css, /\.guide-step-detour\s*{/);
  assert.match(
    css,
    /\.guide-step-detour-item\s*{[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) auto;/,
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
