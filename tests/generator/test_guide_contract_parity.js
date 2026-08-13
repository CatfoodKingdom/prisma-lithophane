"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { createFeatureHarness } = require("./support/application_harness.cjs");

const fixture = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "guide_contract_before_modular_cutover.json"),
  "utf8",
));

const LAUNCH_IDS = [
  "guided-setup",
  "prisma-generator-basics",
  "image-guide",
  "palette-guide",
  "settings-guide",
  "preview-guide",
  "export-guide",
  "guided-setup-help-pointer",
  "interface-preview",
];

function normalizedContract(guide) {
  const copy = JSON.parse(JSON.stringify(guide));
  for (const key of [
    "kind", "workspace_policy", "canonical_guide_id", "route_id", "baseline",
    "preparation_actions", "routes", "launch_aliases", "prepare_id", "preparation",
  ]) delete copy[key];
  for (const current of [
    ...(copy.steps || []),
    ...(copy.detours || []).flatMap(detour => detour.steps || []),
  ]) {
    delete current.enter_actions;
    delete current.complete_actions;
  }
  return copy;
}

function migratePrintSetupSteps(contract) {
  const renamed = new Map([
    ["active-nozzle", "active-extrusion-width"],
    ["tutorial-nozzle", "tutorial-extrusion-width"],
  ]);
  for (const step of contract.steps || []) {
    step.id = renamed.get(step.id) || step.id;
    if (step.target_id === "sidebar.active-nozzle") step.target_id = "sidebar.active-extrusion-width";
    if (step.reveal_id === "sidebar.active-nozzle") step.reveal_id = "sidebar.active-extrusion-width";
  }
  for (const chapter of contract.chapters || []) {
    chapter.step_ids = (chapter.step_ids || []).map(stepId => renamed.get(stepId) || stepId);
  }
  return contract;
}

test("modular guide launches preserve the characterized pre-cutover contract", async () => {
  const { app } = await createFeatureHarness();
  const before = new Map(fixture.definitions.map(guide => [guide.id, normalizedContract(guide)]));

  for (const launchId of LAUNCH_IDS) {
    const actual = normalizedContract(app.commands.getGuideDefinition(launchId));
    const expected = migratePrintSetupSteps(before.get(launchId));
    assert.ok(expected, `missing characterization fixture for ${launchId}`);

    if ([
      "prisma-generator-basics", "image-guide", "palette-guide", "settings-guide",
      "preview-guide", "export-guide",
    ].includes(launchId)) {
      actual.version = expected.version;
      const approvedCopyStepIds = new Set([
        "printer-select",
        "tutorial-extrusion-width",
        "image-introduction",
        "image-library",
        "image-library-maintenance",
        "choose-image",
        "image-preview",
        "aspect-ratios",
        "interactive-framing",
        "physical-dimensions",
        "border",
        "appearance",
        "reset-image-controls",
        "tutorial-canvas",
        "open-palette",
        "palette-introduction",
        "palette-methods",
        "autosuggest-controls",
        "suggest-palettes",
        "add-suggestions",
        "palette-deck",
        "manual-palette-remove",
        "settings-introduction",
        "open-settings",
        "settings-profile",
        "preprocessing-solver",
        "white-cap",
        "advanced-settings",
        "close-settings",
        "settings-guide-complete",
      ]);
      for (const actualStep of actual.steps) {
        const expectedStep = expected.steps.find(step => step.id === actualStep.id);
        if (!expectedStep) continue;
        if (approvedCopyStepIds.has(actualStep.id)) expectedStep.body = actualStep.body;
        if (["workflow", "printer-select", "tutorial-extrusion-width", "open-palette"].includes(actualStep.id)) {
          expectedStep.viewport_anchor = actualStep.viewport_anchor;
        }
        if (actualStep.id === "tutorial-extrusion-width") {
          expectedStep.title = actualStep.title;
          expectedStep.target_id = actualStep.target_id;
          expectedStep.reveal_id = actualStep.reveal_id;
          expectedStep.completion = actualStep.completion;
        }
        if (actualStep.id === "image-introduction") {
          expectedStep.target_id = actualStep.target_id;
          expectedStep.overlay_mode = actualStep.overlay_mode;
          expectedStep.preferred_placements = actualStep.preferred_placements;
        }
        if (actualStep.id === "image-library") {
          expectedStep.title = actualStep.title;
          expectedStep.note = actualStep.note;
        }
        if (actualStep.id === "settings-profile") {
          expectedStep.title = actualStep.title;
          expectedStep.target_id = actualStep.target_id;
          expectedStep.reveal_id = actualStep.reveal_id;
        }
        if (actualStep.id === "preprocessing-solver") expectedStep.title = actualStep.title;
      }
    }

    if (["prisma-generator-basics", "settings-guide"].includes(launchId)) {
      const addedSettingsSteps = new Set([
        "lithophane-stack",
        "settings-solve-resolution",
        "settings-layer-construction",
        "settings-profiles",
      ]);
      actual.steps = actual.steps.filter(step => !addedSettingsSteps.has(step.id));
      const settingsChapter = actual.chapters.find(chapter => chapter.id === "settings");
      settingsChapter.step_ids = settingsChapter.step_ids.filter(
        stepId => !addedSettingsSteps.has(stepId),
      );
      if (launchId === "settings-guide") expected.summary = actual.summary;
    }

    if (launchId === "prisma-generator-basics") {
      const addedIntroductionSteps = new Set([
        "lithophane-image-principle",
        "prisma-creation-overview",
      ]);
      actual.steps = actual.steps.filter(step => !addedIntroductionSteps.has(step.id));
      const actualIntroduction = actual.chapters.find(chapter => chapter.id === "introduction");
      actualIntroduction.step_ids = actualIntroduction.step_ids.filter(
        stepId => !addedIntroductionSteps.has(stepId),
      );
      const actualCompletion = actual.steps.find(step => step.id === "complete");
      const expectedCompletion = expected.steps.find(step => step.id === "complete");
      assert.match(actualCompletion.body, /restore your previous Settings Profile, printer, Extrusion Width/);
      assert.match(expectedCompletion.body, /Tutorial Printer profile and its 0\.4 mm nozzle will remain active/);
      actualCompletion.body = expectedCompletion.body;
      expected.detours = actual.detours;
    }

    if (launchId === "palette-guide") {
      const addedPaletteSteps = new Set([
        "open-palette-variant",
        "add-palette-variant",
        "remove-palette-variant",
        "palette-saving-pointer",
      ]);
      actual.steps = actual.steps.filter(step => !addedPaletteSteps.has(step.id));
      const paletteChapter = actual.chapters.find(chapter => chapter.id === "palette");
      paletteChapter.step_ids = paletteChapter.step_ids.filter(
        stepId => !addedPaletteSteps.has(stepId),
      );
    }

    if (launchId === "preview-guide") {
      const removedPreviewSteps = new Set([
        "closer-inspection",
        "captured-settings",
      ]);
      expected.steps = expected.steps.filter(step => !removedPreviewSteps.has(step.id));
      const previewChapter = expected.chapters.find(chapter => chapter.id === "preview");
      previewChapter.step_ids = previewChapter.step_ids.filter(
        stepId => !removedPreviewSteps.has(stepId),
      );
      for (const stepId of ["solve-history", "preview-views", "preview-guide-complete"]) {
        const actualStep = actual.steps.find(step => step.id === stepId);
        const expectedStep = expected.steps.find(step => step.id === stepId);
        expectedStep.title = actualStep.title;
        expectedStep.body = actualStep.body;
      }
    }

    if (launchId === "guided-setup") {
      // First-Time Setup copy, navigation, and card placement were intentionally
      // refined after the modular cutover; focused foundation tests pin them.
      expected.title = actual.title;
      for (const actualStep of actual.steps) {
        const expectedStep = expected.steps.find(step => step.id === actualStep.id);
        assert.ok(expectedStep, `missing migrated First-Time Setup step ${actualStep.id}`);
        expectedStep.title = actualStep.title;
        expectedStep.body = actualStep.body;
        expectedStep.allow_previous = actualStep.allow_previous;
        if (actualStep.viewport_anchor) expectedStep.viewport_anchor = actualStep.viewport_anchor;
        if (actualStep.followup) expectedStep.followup = actualStep.followup;
      }
    }

    if (launchId === "guided-setup-help-pointer") {
      expected.summary = actual.summary;
      expected.steps[0].body = actual.steps[0].body;
    }

    assert.deepEqual(actual, expected, `${launchId} changed outside the approved cutover differences`);
  }
});

test("focused launch IDs resolve to routes on one canonical Basics guide", async () => {
  const { app } = await createFeatureHarness();
  const basics = app.commands.getGuideDefinition("prisma-generator-basics");
  assert.deepEqual(Object.keys(basics.routes), ["full", "image", "palette", "settings", "preview", "export"]);
  for (const launchId of LAUNCH_IDS.slice(2, 7)) {
    const launch = app.commands.resolveGuideLaunch(launchId);
    assert.equal(launch.guide_id, "prisma-generator-basics");
    assert.equal(app.commands.getGuideDefinition(launchId).route_id, launch.route_id);
  }
});

function settingsDrawerScriptCopy() {
  const markdown = fs.readFileSync(path.join(
    __dirname,
    "../../Prisma/generator/docs/planned_guides/settings_drawer/SETTINGS_DRAWER_SCRIPT.md",
  ), "utf8");
  const lines = markdown.split(/\r?\n/);
  const copy = new Map();
  for (let index = 0; index < lines.length; index += 1) {
    const idMatch = lines[index].match(/^\*\*Step ID:\*\* `([^`]+)`$/);
    if (!idMatch) continue;
    while (index < lines.length && lines[index] !== "**Guide text:**") index += 1;
    index += 1;
    while (lines[index] === "") index += 1;
    const quoted = [];
    while (index < lines.length && (lines[index].startsWith(">") || lines[index] === "")) {
      if (lines[index].startsWith(">")) quoted.push(lines[index].replace(/^> ?/, ""));
      else if (quoted.length && quoted.at(-1) !== "") quoted.push("");
      index += 1;
    }
    while (quoted.at(-1) === "") quoted.pop();
    const title = quoted.shift().replace(/^\*\*(.*)\*\*$/, "$1");
    while (quoted[0] === "") quoted.shift();
    const paragraphs = [];
    let paragraph = [];
    for (const line of quoted) {
      if (line) paragraph.push(line);
      else if (paragraph.length) {
        paragraphs.push(paragraph.join(" "));
        paragraph = [];
      }
    }
    if (paragraph.length) paragraphs.push(paragraph.join(" "));
    copy.set(idMatch[1], { title, body: paragraphs.join("\n\n") });
  }
  return copy;
}

test("Settings Drawer learner copy matches its authoritative script", async () => {
  const { app } = await createFeatureHarness();
  const guide = app.commands.getGuideDefinition("settings-drawer");
  const expected = settingsDrawerScriptCopy();
  const steps = app.commands.getAllGuideSteps(guide);
  assert.equal(expected.size, 59);
  assert.equal(steps.length, 59);
  for (const current of steps) {
    assert.deepEqual(
      { title: current.title, body: current.body },
      expected.get(current.id),
      current.id,
    );
  }
});
