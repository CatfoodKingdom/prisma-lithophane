"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  createFeatureHarness,
  fakeElement,
  memoryStorage,
  moduleUrl,
} = require("./support/application_harness.cjs");

const filaments = [
  { filament_id: "white", display_name: "White", has_profile: true },
  { filament_id: "red", display_name: "Red", has_profile: true },
  { filament_id: "blue", display_name: "Blue", has_profile: true },
  { filament_id: "excluded", display_name: "Excluded", has_profile: true, exclude_from_model: true },
  { filament_id: "uncalibrated", display_name: "Uncalibrated", has_profile: false },
];

async function harness(options = {}) {
  return createFeatureHarness({ filaments, ...options });
}

test("profile application overwrites owned values and drops retired aliases", async () => {
  const { app } = await harness();
  app.state.settings.config.solver_fine_pitch_mm = 0.8;
  app.state.settings.config.image_sample_pitch_mm = 0.7;
  app.commands._applySettingsProfileToConfig({
    solver_fine_pitch_mm: 0.4,
    image_sample_pitch_mm: 0.3,
    gamut_mode: "chroma",
    guided_surface_enabled: true,
    tv_weight: 99,
  });
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.image_sample_pitch_mm, 0.3);
  assert.equal(app.state.settings.config.gamut_mode, "hue_preserving");
  assert.equal("guided_surface_enabled" in app.state.settings.config, false);
  assert.equal("tv_weight" in app.state.settings.config, false);
});

test("profile keys own current controls but not mandatory product safety", async () => {
  const { app } = await harness();
  const keys = app.state.settings.SETTINGS_PROFILE_KEYS;
  assert.ok(keys.includes("luminance_mode"));
  assert.ok(keys.includes("detail_cap_max_layers"));
  assert.ok(keys.includes("color_region_target_mm"));
  assert.ok(keys.includes("neutral_field_protection_mode"));
  assert.ok(keys.includes("neutral_field_protection_cutoff"));
  assert.ok(!keys.includes("use_corrections"));
  assert.ok(!keys.includes("stage2_boundary_mutation_current_de_percentile"));
  assert.ok(!keys.includes("stage2_boundary_mutation_min_component_mm"));
  assert.ok(!keys.includes("enforce_printability"));
  assert.ok(!keys.includes("cap_continuity_cleanup"));
  assert.ok(!keys.includes("printability_minimum_extrusion_width_mm"));
  assert.ok(!keys.includes("printability_minimum_line_length_mm"));
  assert.ok(!keys.includes("min_line_length_multiplier"));
  assert.equal(app.state.settings.config.enforce_printability, true);
  assert.equal(app.state.settings.config.cap_continuity_cleanup, true);
});

test("settings numeric display values are rounded without inventing precision", async () => {
  const { app } = await harness();
  assert.equal(app.commands.formatSettingsInputNumber(0.7999999999999999), "0.8");
  assert.equal(app.commands.formatSettingsInputNumber(0.004000000000000001), "0.004");
  assert.equal(app.commands.formatSettingsInputNumber(75), "75");
  assert.equal(app.commands.formatSettingsInputNumber(""), "");
});

test("settings wheel direction increases on upward gestures", async () => {
  const { app } = await harness();
  assert.equal(app.commands.settingsWheelMultiplier(-1), 1);
  assert.equal(app.commands.settingsWheelMultiplier(1), -1);
  assert.equal(app.commands.settingsWheelMultiplier(0), 0);
});

test("settings steppers are limited to genuinely quantized controls", async () => {
  const { app } = await harness();
  for (const controlId of [
    "cfgDWcMin",
    "cfgKMax",
    "cfgStage1Coarsening",
    "cfgStage2BoundaryMutationMaxPasses",
    "cfgBaseShadingLimit",
    "cfgDetailCapMaxLayers",
  ]) {
    assert.equal(app.commands.settingsNumericRuleFor(controlId).quantized, true);
  }
  for (const controlId of [
    "cfgDeThreshold",
    "cfgColorRegionTarget",
    "cfgStage2BoundaryMutationMinGain",
    "cfgBoundaryCapDeBudget",
    "cfgSmoothKernel",
  ]) {
    assert.equal(app.commands.settingsNumericRuleFor(controlId), null);
  }
});

test("settings presentation follows the drawer hierarchy", async () => {
  const { app } = await harness();
  const solver = app.state.ui.SETTINGS_PRESENTATION.find((section) => section.key === "solver");
  const whiteCap = app.state.ui.SETTINGS_PRESENTATION.find((section) => section.key === "white-cap");
  assert.equal(solver.rows.some((row) => row.key === "luminance_base_shading_limit_fraction"), false);
  assert.deepEqual(
    whiteCap.rows.find((row) => row.key === "luminance_base_shading_limit_fraction"),
    {
      key: "luminance_base_shading_limit_fraction",
      controlId: "cfgBaseShadingLimit",
      label: "Shading balance",
      format: "percent",
      default: 0.75,
      group: "Boundary",
    },
  );
});

test("neutral-field protection defaults off and round-trips through the settings UI", async () => {
  const neutralFieldSelect = fakeElement();
  neutralFieldSelect.value = "broad";
  const neutralCutoff = fakeElement();
  neutralCutoff.value = "0.035";
  const { app } = await harness({
    elements: {
      "#cfgNeutralFieldProtection": neutralFieldSelect,
      "#cfgNeutralFieldProtectionCutoff": neutralCutoff,
    },
  });
  assert.equal(app.state.settings.SETTINGS_PROFILE_DEFAULTS.neutral_field_protection_mode, "off");
  app.commands.syncSettingsPresentationMetadata();
  assert.equal(neutralFieldSelect.dataset.settingsKey, "neutral_field_protection_mode");
  assert.equal(neutralFieldSelect.dataset.settingsSection, "solver");

  global.document = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  try {
    app.commands.readConfigFromUI();
    assert.equal(app.state.settings.config.neutral_field_protection_mode, "broad");
    assert.equal(app.state.settings.config.neutral_field_protection_cutoff, 0.035);

    app.commands._applySettingsProfileToConfig({
      neutral_field_protection_mode: "standard",
      neutral_field_protection_cutoff: 0.027,
    });
    assert.equal(app.state.settings.config.neutral_field_protection_mode, "standard");
    assert.equal(app.state.settings.config.neutral_field_protection_cutoff, 0.027);
    neutralFieldSelect.value = "standard";
    app.commands._setLoadedSettingsProfile(
      { id: "neutral", kind: "named", name: "Neutral" },
      {
        settings: app.commands._currentSettingsSnapshot(),
        modules: app.commands._currentSettingsProfileModulesSnapshot(),
      },
    );
    assert.equal(app.commands.isSettingsProfileModified(), false);
    neutralFieldSelect.value = "broad";
    assert.equal(app.commands.isSettingsProfileModified(), true);
    neutralFieldSelect.value = "standard";
    app.state.settings.config.neutral_field_protection_mode = "standard";
    const solverSection = app.state.ui.READ_ONLY_RUN_SETTING_SECTIONS.find(
      (section) => section.key === "solver",
    );
    const rows = app.commands.buildReadOnlyRunSectionRows(
      solverSection,
      { settings: { neutral_field_protection_mode: "standard" } },
    );
    assert.equal(
      rows.find((row) => row.label === "Neutral-field protection").value,
      "Standard",
    );

    const legacyRun = { label: "Legacy", config: {} };
    const explicitOffRun = {
      label: "Explicit Off",
      config: { neutral_field_protection_mode: "off" },
    };
    assert.equal(
      app.commands.getSolveRunSettingsSnapshot(legacyRun).neutral_field_protection_mode,
      "off",
    );
    assert.equal(
      app.commands.getSolveRunSettingsSnapshot({
        config: { neutral_field_protection_mode: "standard" },
        recipe_snapshot: { profile_snapshot: { settings: {} } },
      }).neutral_field_protection_mode,
      "standard",
    );
    assert.equal(
      app.commands.getFrozenSolveRunSnapshot(legacyRun).settings.neutral_field_protection_mode,
      "off",
    );
    assert.equal(
      app.commands.collectSolveRunSettingDiffs(legacyRun, explicitOffRun)
        .some((diff) => diff.label === "Neutral-field protection"),
      false,
    );
    const modeDiff = app.commands.collectSolveRunSettingDiffs(
      explicitOffRun,
      { label: "Standard", config: { neutral_field_protection_mode: "standard" } },
    ).find((diff) => diff.label === "Neutral-field protection");
    assert.deepEqual(
      { before: modeDiff.before, after: modeDiff.after, category: modeDiff.category },
      { before: "Off", after: "Standard", category: "solver" },
    );

    app.commands._applySettingsProfileToConfig({});
    assert.equal(app.state.settings.config.neutral_field_protection_mode, "off");
  } finally {
    delete global.document;
  }
});

test("module snapshots use descriptor defaults and compare nested values", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [
    { name: "demo", slot: "preprocessing", default_enabled: true },
    { name: "off", slot: "preprocessing", default_enabled: false },
  ];
  assert.deepEqual(app.commands._normalizeSettingsProfileModules({}, {}), { demo: true, off: false });
  assert.equal(app.commands._settingsProfileModulesEqual({ demo: true }, { demo: true }), true);
  assert.equal(app.commands._settingsProfileModulesEqual({ demo: true }, { demo: false }), false);
  assert.equal(
    app.commands._settingsProfileValuesEqual(
      { preprocessing_params: { demo: { radius: 2 } } },
      { preprocessing_params: { demo: { radius: 2 } } },
    ),
    true,
  );
});

test("temporary settings profiles prefer durable run snapshots", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [
    { name: "denoise", slot: "preprocessing", default_enabled: false },
  ];
  const profile = app.commands.buildTemporarySettingsProfileFromRun({
    id: "run-1",
    config: { t_max: 9 },
    recipe_snapshot: {
      profile_snapshot: {
        settings: { t_max: 3, solver_fine_pitch_mm: 0.4 },
        modules: { denoise: true },
      },
    },
  }, { label: "Loaded run" });
  assert.equal(profile.kind, "temporary");
  assert.equal(profile.settings.t_max, 3);
  assert.equal(profile.settings.solver_fine_pitch_mm, 0.4);
  assert.deepEqual(profile.modules, { denoise: true });
});

test("white-point rescale is basic, grouped in run settings, and summarized only when enabled", async () => {
  const { app } = await harness();
  const run = {
    id: "run-1",
    label: "Rescaled portrait",
    config: {
      luminance_mode: "standard",
      solver_fine_pitch_mm: 0.4,
      image_sample_pitch_mm: 0.4,
      t_max: 3,
      layer_height: 0.08,
      base_filament: "white",
      d_wb: 0.2,
      d_wc_min: 0.16,
      smooth_kernel: 2.5,
      detail_cap_max_layers: 5,
      appearance_model_provider: "photo_stack_bundle",
      use_corrections: true,
      k_max: 3,
      color_region_target_mm: 0.6,
      gamut_white_rescale: true,
    },
  };

  const summary = app.commands.getSolveRunEssentialsItems(run);
  assert.deepEqual(
    summary.find((item) => item.label === "White-point rescale"),
    { label: "White-point rescale", value: "On" },
  );
  assert.equal(
    app.commands.getSolveRunEssentialsItems({
      ...run,
      config: { ...run.config, gamut_white_rescale: false },
    }).some((item) => item.label === "White-point rescale"),
    false,
  );

  const solverSection = app.state.ui.READ_ONLY_RUN_SETTING_SECTIONS.find(
    (section) => section.key === "solver",
  );
  const frozen = app.commands.getFrozenSolveRunSnapshot(run);
  const rows = app.commands.buildReadOnlyRunSectionRows(solverSection, frozen);
  assert.equal(rows.find((row) => row.label === "White-point rescale").advanced, false);

  app.commands.esc = (value) => String(value);
  const html = app.commands.buildReadOnlyRunSettingsHtml(run);
  assert.match(html, /class="run-settings-sections"/);
  assert.match(html, /<h4 class="settings-group-cap run-settings-section-cap">Color Solver<\/h4>/);
  assert.match(html, /<h5 class="run-settings-subsection-cap">Recipe Search<\/h5>/);
  const whitePointIndex = html.indexOf("White-point rescale");
  const whitePointRowStart = html.lastIndexOf('<div class="run-settings-row', whitePointIndex);
  assert.ok(whitePointRowStart >= 0);
  assert.equal(
    html.slice(whitePointRowStart, whitePointIndex).includes("is-advanced"),
    false,
  );
});

test("settings columns preserve order while balancing subsection flow units", async () => {
  const { app } = await harness();
  const items = Array.from({ length: 10 }, (_, index) => ({ index }));
  const heights = [98, 1, 225, 138, 61, 111, 59, 61, 59, 59];

  const columns = app.commands.partitionSettingsItems(items, heights, 2, 456);
  assert.deepEqual(
    columns.map((column) => column.map((item) => item.index)),
    [[0, 1, 2, 3], [4, 5, 6, 7, 8, 9]],
  );
  assert.deepEqual(
    app.commands.partitionSettingsItems(items, heights, 3, 1000),
    [items],
  );
});

test("chroma controls round-trip raw multipliers without quantization", async () => {
  const { app } = await harness();
  for (let quarterSteps = -12; quarterSteps <= 12; quarterSteps += 1) {
    const position = quarterSteps / 4;
    const weight = app.commands.chromaWeightFromSliderPosition(position);
    assert.ok(Math.abs(app.commands.chromaWeightToSliderPosition(weight) - position) < 1e-12);
  }
  assert.equal(app.commands.formatChromaWeightReadout(5), "5.00");
  assert.equal(app.commands.normalizeChromaWeight(0), 1);
});

test("luminance mode expands to authoritative backend flags", async () => {
  const { app } = await harness();
  app.commands.applyLuminanceMode("luminance_detail");
  assert.equal(app.state.settings.config.luminance_mode, "luminance_detail");
  assert.equal(app.state.settings.config.luminance_handler_enabled, true);
  assert.equal(app.state.settings.config.luminance_handler_mode, "boundary_ceiling");
  assert.equal(app.state.settings.config.enforce_printability, true);
  assert.equal(app.state.settings.config.luminance_detail_authoring_printability, "absolute_finalgate");
  app.commands.applyLuminanceMode("standard", { resetStandard: true });
  assert.equal(app.state.settings.config.luminance_handler_enabled, false);
  assert.equal(app.state.settings.config.luminance_detail_authoring_printability, "off");
});

test("solve preflight math reports layer alignment and nozzle constraints", async () => {
  const { app } = await harness();
  const aligned = app.commands.calculateStackLayerAlignment(0.08, 0.2, 0.08, 2.28);
  assert.equal(aligned.remainderMm, 0);
  const misaligned = app.commands.calculateStackLayerAlignment(0.08, 0.2, 0.08, 2.3);
  assert.ok(misaligned.remainderMm > 0);
  assert.match(app.commands.buildStackLayerAlignmentIssue(misaligned), /whole Layer Height steps/);
  assert.match(app.commands.buildSolvePitchNozzleIssue(0.2, 0.4), /cannot be smaller/);
});

test("solve pitch mismatch detection distinguishes invalid, equal, and coarser pitches", async () => {
  const solvePitch = fakeElement();
  solvePitch.value = "0.2";
  const { app } = await harness({ elements: { "#cfgSolvePitch": solvePitch } });
  app.state.session.activeNozzle = { size: 0.4 };

  assert.deepEqual(app.commands.getSolvePitchNozzleMismatch(), {
    pitch: 0.2,
    nozzleSize: 0.4,
    message: app.commands.buildSolvePitchNozzleIssue(0.2, 0.4),
  });
  solvePitch.value = "0.4";
  assert.equal(app.commands.getSolvePitchNozzleMismatch(), null);
  solvePitch.value = "0.6";
  assert.equal(app.commands.getSolvePitchNozzleMismatch(), null);
  solvePitch.value = "0.3999995";
  assert.equal(app.commands.getSolvePitchNozzleMismatch(), null);
});

test("editing solve pitch preserves the physical smoothing radius", async () => {
  const solvePitch = fakeElement();
  solvePitch.value = "0.4"; // The browser updates the field before dispatching input.
  const smoothRadius = fakeElement();
  const { app } = await harness({ elements: {
    "#cfgSolvePitch": solvePitch,
    "#cfgSmoothKernel": smoothRadius,
  } });
  app.state.settings.config.solver_fine_pitch_mm = 0.2;
  app.state.settings.config.image_sample_pitch_mm = 0.2;
  app.state.settings.config.smooth_kernel = 5;

  assert.equal(app.commands.applySolvePitchDraft(solvePitch.value), true);

  assert.equal(smoothRadius.value, 1);
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.image_sample_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.smooth_kernel, 2.5);
  assert.equal(
    app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel, 0.4),
    1,
  );
});

test("solve pitch remediation preserves smoothing radius and synchronizes the canonical setting", async () => {
  const solvePitch = fakeElement();
  solvePitch.value = "0.2";
  const layerHeight = fakeElement();
  layerHeight.value = "0.08";
  const baseThickness = fakeElement();
  baseThickness.value = "0.2";
  const capLayers = fakeElement();
  capLayers.value = "2";
  const maxThickness = fakeElement();
  maxThickness.value = "3";
  const smoothRadius = fakeElement();
  const { app } = await harness({ elements: {
    "#cfgSolvePitch": solvePitch,
    "#cfgLayerHeight": layerHeight,
    "#cfgDWb": baseThickness,
    "#cfgDWcMin": capLayers,
    "#cfgTMax": maxThickness,
    "#cfgSmoothKernel": smoothRadius,
  } });
  app.state.session.activeNozzle = {
    size: 0.4,
    min_layer_height: 0.04,
    max_layer_height: 0.2,
  };
  app.state.settings.config.solver_fine_pitch_mm = 0.2;
  app.state.settings.config.image_sample_pitch_mm = 0.2;
  app.state.settings.config.smooth_kernel = 5;
  const initialRadius = app.commands.smoothingRadiusMmFromCells(5, 0.2);
  let dialogOptions = null;
  let syncs = 0;
  app.commands.appConfirm = async (_message, options) => {
    dialogOptions = options;
    return true;
  };
  app.commands.updateDerivedParams = () => {};
  app.commands.syncConfigToServer = async () => { syncs += 1; };

  const result = await app.commands.syncSolveSettingsWithPitchRemediation({ intent: "single" });

  assert.deepEqual(result, { proceed: true, corrected: true });
  assert.equal(dialogOptions.title, "Solve Pitch Is Too Small");
  assert.equal(dialogOptions.ok, "Use 0.4 mm and Solve");
  assert.equal(solvePitch.value, "0.4");
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.image_sample_pitch_mm, 0.4);
  assert.equal(
    app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel, 0.4),
    initialRadius,
  );
  assert.equal(syncs, 1);
});

test("solve pitch remediation cancels without mutation and uses Continue when other errors remain", async () => {
  const solvePitch = fakeElement();
  solvePitch.value = "0.2";
  const { app } = await harness({ elements: { "#cfgSolvePitch": solvePitch } });
  app.state.session.activeNozzle = { size: 0.4 };
  app.state.settings.config.solver_fine_pitch_mm = 0.2;
  app.state.settings.config.image_sample_pitch_mm = 0.2;
  let dialogOptions = null;
  let syncs = 0;
  const mismatchMessage = app.commands.buildSolvePitchNozzleIssue(0.2, 0.4);
  app.commands.getSolveSettingsPreflightIssues = () => [
    mismatchMessage,
    "Max Total Thickness is not aligned",
  ];
  app.commands.appConfirm = async (_message, options) => {
    dialogOptions = options;
    return false;
  };
  app.commands.syncConfigToServer = async () => { syncs += 1; };

  const result = await app.commands.syncSolveSettingsWithPitchRemediation({ intent: "single" });

  assert.deepEqual(result, { proceed: false, corrected: false });
  assert.equal(dialogOptions.ok, "Use 0.4 mm and Continue");
  assert.equal(solvePitch.value, "0.2");
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.2);
  assert.equal(syncs, 0);
});

test("confirmation dialog traps focus, cancels on Escape, and restores prior focus", async () => {
  const overlay = fakeElement();
  const title = fakeElement();
  const message = fakeElement();
  const input = fakeElement();
  const buttons = fakeElement();
  const closeButton = fakeElement();
  const cancelButton = fakeElement();
  const confirmButton = fakeElement();
  const hint = fakeElement();
  const listeners = new Map();
  let restoredFocus = 0;
  const previousFocus = {
    focus() {
      restoredFocus += 1;
      dialogDocument.activeElement = previousFocus;
    },
  };
  const dialogDocument = {
    activeElement: previousFocus,
    body: { contains: () => true },
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
  overlay.ownerDocument = dialogDocument;
  for (const element of [closeButton, cancelButton, confirmButton]) {
    element.focus = () => { dialogDocument.activeElement = element; };
  }
  const { app } = await harness({ elements: {
    "#appDialog": overlay,
    "#appDialogTitle": title,
    "#appDialogMsg": message,
    "#appDialogInput": input,
    "#appDialogButtons": buttons,
    "#appDialogClose": closeButton,
    "#appDialogNo": cancelButton,
    "#appDialogYes": confirmButton,
    "#appDialogHint": hint,
  } });
  app.commands.esc2 = value => String(value);

  const resultPromise = app.commands.appConfirm("Proceed?", {
    title: "Confirm Action",
    ok: "Proceed",
    emphasis: ["Proceed"],
  });
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(dialogDocument.activeElement, confirmButton);
  assert.equal(
    message.innerHTML,
    '<strong class="app-dialog-emphasis">Proceed</strong>?',
  );

  let tabPrevented = false;
  listeners.get("keydown")({
    key: "Tab",
    shiftKey: false,
    preventDefault() { tabPrevented = true; },
  });
  assert.equal(tabPrevented, true);
  assert.equal(dialogDocument.activeElement, closeButton);

  let escapePrevented = false;
  listeners.get("keydown")({
    key: "Escape",
    preventDefault() { escapePrevented = true; },
  });
  assert.equal(await resultPromise, false);
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(escapePrevented, true);
  assert.equal(overlay.getAttribute("aria-hidden"), "true");
  assert.equal(restoredFocus, 1);
  assert.equal(listeners.has("keydown"), false);
});

test("printer printability uses bounded whole-nozzle length multipliers", async () => {
  const { app } = await harness();
  assert.deepEqual(app.commands.defaultNozzleLineWidths(0.2), {
    line_width: 0.22,
    max_line_width: 0.25,
    min_line_length_multiplier: 2,
  });
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.2, 2), "0.4");
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.4, 10), "4");
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.3333333, 3), "0.999999");

  const normalized = app.commands.normalizeNozzleProfile({
    size: 0.4,
    min_layer_height: 0.08,
    max_layer_height: 0.32,
    line_width: 0.2,
    max_line_width: 0.3,
    min_line_length_multiplier: 3,
  });
  assert.equal(normalized.line_width, 0.4);
  assert.equal(normalized.max_line_width, 0.4);
  assert.equal(normalized.min_line_length_multiplier, 3);
  assert.equal("min_line_width" in normalized, false);
  assert.equal("min_line_length" in normalized, false);

  const sizeInput = fakeElement();
  sizeInput.value = "0.2";
  sizeInput.setCustomValidity = message => { sizeInput.validationMessage = message; };
  const multiplierInput = fakeElement();
  multiplierInput.value = "1";
  multiplierInput.setCustomValidity = message => { multiplierInput.validationMessage = message; };
  const output = fakeElement();
  const row = fakeElement();
  row.querySelector = selector => ({
    ".nz-size": sizeInput,
    ".nz-min-ll-mult": multiplierInput,
    ".nz-min-ll-derived": output,
  }[selector] || null);
  assert.equal(app.commands.validateNozzleRow(row), false);
  assert.match(multiplierInput.validationMessage, /whole number from 2 through 10/);
  assert.equal(row.classList.contains("is-invalid"), true);

  multiplierInput.value = "3";
  app.commands.syncNozzleDerivedLength(row);
  assert.equal(output.textContent, "× nozzle = 0.6 mm");
  assert.equal(app.commands.validateNozzleRow(row), true);
  assert.equal(multiplierInput.validationMessage, "");
  assert.equal(row.classList.contains("is-invalid"), false);
});

test("live config uses server-authoritative active printer thresholds", async () => {
  const { app } = await harness();
  app.state.session.activeNozzle = {
    size: 0.4,
    min_line_length_multiplier: 2,
  };
  app.state.session.activePrintability = {
    minimum_extrusion_width_mm: 0.4,
    minimum_line_length_mm: 0.8,
    minimum_component_area_mm2: 0.32,
  };
  global.document = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  try {
    app.commands.readConfigFromUI();
    assert.equal(app.state.settings.config.printability_minimum_extrusion_width_mm, 0.4);
    assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.8);
  } finally {
    delete global.document;
  }
});

test("opening printer configuration emits its semantic event after rendering", async () => {
  const printerPage = fakeElement();
  printerPage.classList.add("is-hidden");
  const { app } = await harness({
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = {
    printers: [],
    active_printer_id: null,
  };
  app.commands.renderPrinterConfigPage = () => {};
  const events = [];
  const dispose = app.events.subscribe("printer-config.opened", detail => events.push(detail));

  try {
    app.commands.showPrinterConfigPage();
  } finally {
    dispose();
  }

  assert.equal(printerPage.classList.contains("is-hidden"), false);
  assert.deepEqual(events, [{ source: "printer-configuration" }]);
});

test("switching adjustment tabs emits its semantic event after state and controls update", async () => {
  const sizeControls = fakeElement();
  const imageControls = fakeElement();
  const canvas = fakeElement();
  const { app } = await harness({
    elements: {
      "#frameControlsSize": sizeControls,
      "#frameControlsImage": imageControls,
      "#frameCanvas": canvas,
    },
  });
  const events = [];
  const dispose = app.events.subscribe(
    "image.adjustment-tab.changed",
    detail => events.push({
      ...detail,
      state: app.state.image.frameEditorTab,
      imageVisible: !imageControls.classList.contains("is-hidden"),
    }),
  );

  try {
    app.commands.switchFrameEditorTab("image");
  } finally {
    dispose();
  }

  assert.equal(sizeControls.classList.contains("is-hidden"), true);
  assert.equal(imageControls.classList.contains("is-hidden"), false);
  assert.equal(canvas.classList.contains("interaction-locked"), true);
  assert.deepEqual(events, [{
    tab: "image",
    state: "image",
    imageVisible: true,
  }]);
});

test("printer editor stays open with its draft after a failed save", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Unsaved draft",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const { app } = await harness({
    api: {
      savePrinters: async () => { throw new Error("offline"); },
    },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.showToast = () => {};
  app.commands.switchTab = () => { throw new Error("must not navigate"); };
  const closeEvents = [];
  const dispose = app.events.subscribe("printer-config.closed", detail => closeEvents.push(detail));

  try {
    assert.equal(await app.commands.hidePrinterConfigPage("image"), false);
  } finally {
    dispose();
  }
  assert.equal(printerPage.classList.contains("is-hidden"), false);
  assert.equal(app.state.session.printersData.printers[0].name, "Unsaved draft");
  assert.equal(app.state.session.printersData.printers[0].nozzle_profiles[0].min_line_length_multiplier, 3);
  assert.deepEqual(closeEvents, []);
});

test("successful printer saves reconcile active printability from the server", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Draft name",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const authoritative = {
    ok: true,
    printers: [{
      id: "printer-a",
      name: "Canonical name",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
    active: {
      printer: { id: "printer-a", name: "Canonical name", max_print_area: { x: 180, y: 180 } },
      nozzle: { size: 0.2, min_line_length_multiplier: 3 },
      printability: {
        minimum_extrusion_width_mm: 0.2,
        minimum_line_length_mm: 0.6,
        minimum_component_area_mm2: 0.12,
      },
    },
  };
  const { app } = await harness({
    api: { savePrinters: async () => authoritative },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.renderPrinterRail = () => {};
  app.commands.updateDerivedParams = () => {};
  app.commands.switchTab = () => {};
  const closeEvents = [];
  const activeEvents = [];
  const dispose = app.events.subscribe("printer-config.closed", detail => closeEvents.push(detail));
  const disposeActive = app.events.subscribe("printer.active-changed", detail => activeEvents.push(detail));

  try {
    assert.equal(await app.commands.hidePrinterConfigPage("image"), true);
  } finally {
    dispose();
    disposeActive();
  }
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.state.session.printersData.printers[0].name, "Canonical name");
  assert.equal(app.state.session.activePrintability.minimum_line_length_mm, 0.6);
  assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.6);
  assert.deepEqual(activeEvents, [{
    printerId: "printer-a",
    source: "printer-configuration",
  }]);
  assert.deepEqual(closeEvents, [{ source: "printer-configuration" }]);
});

test("a post-save render failure never rolls back authoritative printer state", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Draft",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 2 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const authoritative = {
    printers: [{
      id: "printer-a",
      name: "Saved",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 4 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
    active: {
      printer: { id: "printer-a", name: "Saved" },
      nozzle: { size: 0.2, min_line_length_multiplier: 4 },
      printability: {
        minimum_extrusion_width_mm: 0.2,
        minimum_line_length_mm: 0.8,
        minimum_component_area_mm2: 0.16,
      },
    },
  };
  const { app } = await harness({
    api: { savePrinters: async () => authoritative },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.renderPrinterRail = () => {};
  app.commands.updateDerivedParams = () => { throw new Error("render failed"); };
  app.commands.switchTab = () => { throw new Error("must not navigate"); };
  let toast = "";
  app.commands.showToast = message => { toast = message; };
  const originalConsoleError = console.error;
  console.error = () => {};
  try {
    assert.equal(await app.commands.hidePrinterConfigPage("image"), false);
  } finally {
    console.error = originalConsoleError;
  }

  assert.equal(printerPage.classList.contains("is-hidden"), false);
  assert.equal(app.state.session.printersData.printers[0].name, "Saved");
  assert.equal(app.state.session.activePrintability.minimum_line_length_mm, 0.8);
  assert.match(toast, /was saved/);
});

test("filament eligibility and reconciliation preserve explicit choices and default new IDs on", async () => {
  const { app } = await harness();
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[1]), true);
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[3]), false);
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[4]), false);
  assert.deepEqual(
    app.commands.reconcileEnabledFilamentIds(["red", "blue", "new"], {
      eligible_ids: ["red", "blue", "removed"],
      enabled_ids: ["blue", "removed"],
    }),
    ["blue", "new"],
  );
});

test("palette gating distinguishes missing, unavailable, and user-disabled filaments", async () => {
  const { app } = await harness();
  app.state.palette.enabledFilaments = new Set(["white"]);
  const issues = app.commands.getPaletteGatingIssues(["missing", "excluded", "red"]);
  assert.deepEqual(issues, {
    missing: ["missing"], unavailable: ["excluded"], disabled: ["red"],
  });
  const message = app.commands.buildPaletteGatingMessage(issues);
  assert.match(message, /not present/);
  assert.match(message, /unavailable for generation/);
  assert.match(message, /Manage Filaments/);
});

test("palette suggestions use solve-mode names for newly generated cards", async () => {
  const { app } = await harness();
  app.state.palette.stagingDeck = [];
  app.state.palette.nextDeckNum = 1;
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};

  app.commands._processSuggestResults({
    palette_mode: "standard",
    candidates: [{ filament_ids: ["red"], mean_de: 1.2 }],
  });
  app.commands._processSuggestResults({
    palette_mode: "luminance_detail",
    candidates: [{ filament_ids: ["blue"], mean_de: 1.1 }],
  });

  assert.equal(app.state.palette.stagingDeck[0].name, "Color suggested 1");
  assert.equal(app.state.palette.stagingDeck[1].name, "Luminance suggested 2");
  assert.equal("n_out_of_gamut" in app.state.palette.stagingDeck[0].gamut, false);
  assert.equal("total_pixels" in app.state.palette.stagingDeck[0].gamut, false);
});

test("palette suggestion payload requests one exact size without retired tier fields", async () => {
  const paletteColors = fakeElement();
  const suggestionCount = fakeElement();
  const suggestMode = fakeElement();
  paletteColors.value = "6";
  suggestionCount.value = "5";
  suggestMode.value = "standard";
  const { app } = await harness({ elements: {
    "#targetFilamentCount": paletteColors,
    "#targetSuggestCount": suggestionCount,
    "#paletteSuggestMode": suggestMode,
  } });
  app.state.palette.candidateSelection = new Set(["red", "blue"]);

  const payload = app.commands.buildPaletteSuggestionPayload();

  assert.equal(payload.n_filaments, 6);
  assert.equal(payload.top_k, 5);
  assert.equal("max_swaps" in payload, false);
  assert.equal("improvement_threshold" in payload, false);
  assert.equal("force_all_tiers" in payload, false);
});

test("over-capacity palette values are preserved", async () => {
  const paletteColors = fakeElement();
  paletteColors.value = "6";
  const { app } = await harness({ elements: {
    "#targetFilamentCount": paletteColors,
  } });
  app.state.session.printerConfig.ams_slots = 4;

  app.commands.updateSuggestSlotHint();
  assert.equal(paletteColors.value, "6");
  assert.equal(paletteColors.max, "16");

  paletteColors.value = "3";
  app.commands.updateSuggestSlotHint();
  assert.equal(paletteColors.value, "3");

  paletteColors.value = "";
  app.commands.updateSuggestSlotHint();
  assert.equal(paletteColors.value, "3");
});

test("AMS preview separates additional palette colors from physical slots", async () => {
  const paletteColors = fakeElement();
  const preview = fakeElement();
  paletteColors.value = "6";
  const { app } = await harness({ elements: {
    "#targetFilamentCount": paletteColors,
    "#amsPreview": preview,
  } });
  app.state.session.printerConfig = {
    ams_units: 1,
    slots_per_unit: 4,
    ams_slots: 4,
  };
  app.commands.getBaseCapSlots = () => 1;

  app.commands.renderAmsPreview();

  assert.equal((preview.innerHTML.match(/ams-preview-slot is-filled/g) || []).length, 3);
  assert.equal((preview.innerHTML.match(/class="ams-preview-overflow-slot"/g) || []).length, 3);
  assert.equal((preview.innerHTML.match(/class="ams-preview-overflow-pip"/g) || []).length, 3);
  assert.match(preview.innerHTML, /Additional colors \(3\)/);
  assert.match(preview.innerHTML, /May require filament swaps/);
  assert.match(preview.innerHTML, /ams-preview-overflow-slots" style="--ams-preview-columns:4" aria-hidden="true"/);
  assert.match(preview.innerHTML, /3 \/ 3 color slots/);
  assert.match(preview.innerHTML, /--ams-preview-columns:4/);
  assert.equal(preview.role, "img");
  assert.match(preview["aria-label"], /3 additional colors/);
  assert.match(preview["aria-label"], /May require filament swaps/);

  paletteColors.value = "2";
  app.commands.renderAmsPreview();
  assert.equal((preview.innerHTML.match(/ams-preview-slot is-filled/g) || []).length, 2);
  assert.doesNotMatch(preview.innerHTML, /ams-preview-overflow/);
  assert.match(preview.innerHTML, /2 \/ 3 color slots/);

  paletteColors.value = "";
  app.commands.renderAmsPreview();
  assert.equal((preview.innerHTML.match(/ams-preview-slot is-filled/g) || []).length, 0);
  assert.doesNotMatch(preview.innerHTML, /ams-preview-overflow/);
  assert.match(preview.innerHTML, /Select 2–16 palette colors/);
  assert.match(preview["aria-label"], /Select 2 to 16 palette colors/);
});

test("AMS preview respects custom unit geometry and multiple reserved slots", async () => {
  const paletteColors = fakeElement();
  const preview = fakeElement();
  paletteColors.value = "6";
  const { app } = await harness({ elements: {
    "#targetFilamentCount": paletteColors,
    "#amsPreview": preview,
  } });
  app.state.session.printerConfig = {
    ams_units: 2,
    slots_per_unit: 3,
    ams_slots: 6,
  };
  app.commands.getBaseCapSlots = () => 2;

  app.commands.renderAmsPreview();

  assert.equal((preview.innerHTML.match(/ams-preview-unit-label/g) || []).length, 2);
  assert.equal((preview.innerHTML.match(/ams-preview-slot is-white/g) || []).length, 2);
  assert.equal((preview.innerHTML.match(/ams-preview-slot is-filled/g) || []).length, 4);
  assert.equal((preview.innerHTML.match(/class="ams-preview-overflow-pip"/g) || []).length, 2);
  assert.equal((preview.innerHTML.match(/--ams-preview-columns:3/g) || []).length, 3);
  assert.match(preview["aria-label"], /2 slots are reserved for the base and cap/);
  assert.match(preview["aria-label"], /2 additional colors/);
  assert.match(preview["aria-label"], /May require filament swaps/);
});

test("authoritative printer changes refresh the palette capacity preview", async () => {
  const { app } = await harness();
  let hintUpdates = 0;
  let previewRenders = 0;
  app.commands.renderPrinterRail = () => {};
  app.commands.updateDerivedParams = () => {};
  app.commands.updateSuggestSlotHint = () => { hintUpdates += 1; };
  app.commands.renderAmsPreview = () => { previewRenders += 1; };

  app.commands.applyAuthoritativePrinterState(
    { printers: [], active_printer_id: "custom", active_nozzle_size: 0.4 },
    {
      printer: {
        id: "custom",
        name: "Custom",
        max_print_area: { x: 200, y: 200 },
        ams_units: 2,
        slots_per_ams: 3,
      },
      nozzle: { size: 0.4 },
      printability: {},
    },
  );

  assert.equal(app.state.session.printerConfig.ams_slots, 6);
  assert.equal(hintUpdates, 1);
  assert.equal(previewRenders, 1);
});

test("palette suggestion validation requires enough selected eligible colors", async () => {
  const paletteColors = fakeElement();
  paletteColors.value = "3";
  const { app } = await harness({ elements: {
    "#targetFilamentCount": paletteColors,
  } });
  app.state.palette.candidateSelection = new Set(["red", "blue"]);
  let toast = "";
  app.commands.showToast = message => { toast = message; };

  assert.equal(app.commands.validatePaletteSuggestionRequest(), false);
  assert.match(toast, /only 2 eligible color filaments are selected/);

  app.state.palette.candidateSelection.add("green");
  assert.equal(app.commands.validatePaletteSuggestionRequest(), true);
});

test("palette result processing never stages more than Suggestions", async () => {
  const suggestionCount = fakeElement();
  suggestionCount.value = "5";
  const { app } = await harness({ elements: {
    "#targetSuggestCount": suggestionCount,
  } });
  app.state.palette.stagingDeck = [];
  app.state.palette.nextDeckNum = 1;
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};

  app.commands._processSuggestResults({
    palette_mode: "standard",
    candidates: Array.from({ length: 8 }, (_value, index) => ({
      filament_ids: [`unit-${index}`],
      mean_de: 1 + index,
    })),
  });

  assert.equal(app.state.palette.stagingDeck.length, 5);
});

test("auto-suggest shows the reserved base/cap filament without selecting it", async () => {
  const candidateGrid = fakeElement();
  const baseFilament = fakeElement();
  baseFilament.value = "white";
  const paletteFilaments = [
    { filament_id: "white", display_name: "Tough White", color_name: "Tough White", manufacturer: "Bambu", hex: "#ffffff", has_profile: true },
    { filament_id: "red", display_name: "Red", color_name: "Red", manufacturer: "Bambu", hex: "#ff0000", has_profile: true },
    { filament_id: "blue", display_name: "Blue", color_name: "Blue", manufacturer: "Bambu", hex: "#0000ff", has_profile: true },
  ];
  const { app } = await createFeatureHarness({
    filaments: paletteFilaments,
    elements: {
      "#candidateGrid": candidateGrid,
      "#cfgBaseFilament": baseFilament,
    },
  });
  app.state.palette.enabledFilaments = new Set(["white", "red", "blue"]);
  app.state.palette.candidateInitialized = false;
  app.commands.esc = value => String(value);

  app.commands.renderCandidateLibrary();

  assert.deepEqual([...app.state.palette.candidateSelection].sort(), ["blue", "red"]);
  assert.match(candidateGrid.innerHTML, /is-base-cap-reserved/);
  assert.match(candidateGrid.innerHTML, /aria-disabled="true"/);
  assert.match(candidateGrid.innerHTML, /BASE\/CAP/);
  assert.match(candidateGrid.innerHTML, /reserved for the white base and cap/);
});

test("Batch mode seeds the active palette and preserves deck-order selection", async () => {
  const { app } = await harness();
  app.commands.renderDeckCards = () => {};
  app.commands.updateSolveReadiness = () => {};
  app.state.palette.deck = Array.from({ length: 11 }, (_, index) => ({
    id: `deck-${index + 1}`,
    name: `Palette ${index + 1}`,
    filament_ids: index % 2 ? ["red"] : ["blue"],
  }));
  app.state.palette.activeDeckId = "deck-3";

  assert.equal(app.commands.setSolveMode("batch"), true);
  assert.deepEqual([...app.state.solve.batchSelectedDeckIds], ["deck-3"]);
  app.commands.toggleBatchDeckSelection("deck-2");
  app.commands.toggleBatchDeckSelection("deck-1");
  assert.deepEqual(
    app.commands.selectedBatchDeckCards().map(card => card.id),
    ["deck-1", "deck-2", "deck-3"],
  );

  for (let index = 4; index <= 10; index += 1) {
    app.commands.toggleBatchDeckSelection(`deck-${index}`);
  }
  assert.equal(app.state.solve.batchSelectedDeckIds.size, 10);
  assert.equal(app.commands.toggleBatchDeckSelection("deck-11"), false);
  assert.equal(app.state.solve.batchSelectedDeckIds.has("deck-11"), false);

  app.commands.setSolveMode("single");
  app.commands.setSolveMode("batch");
  assert.equal(app.state.solve.batchSelectedDeckIds.size, 10);

  app.state.solve.paletteBatchStartPending = true;
  assert.equal(app.commands.setSolveMode("single"), false);
  assert.equal(app.state.solve.solveMode, "batch");
  app.state.solve.paletteBatchStartPending = false;
});

test("palette variants preserve the source, slot order, and manual draft on cancel", async () => {
  const { app } = await harness();
  app.state.session.allFilaments.push({
    filament_id: "green",
    display_name: "Green",
    has_profile: true,
  });
  app.state.palette.enabledFilaments = new Set(["white", "red", "blue", "green"]);
  app.state.palette.deck = [{
    id: "source",
    name: "Cloud Study",
    filament_ids: ["red", "blue"],
    saved: true,
  }];
  app.state.palette.activeDeckId = "source";
  app.state.palette.manualSlots = ["green"];
  app.commands.appConfirm = async () => true;
  app.commands.switchTab = () => {};
  app.commands.toggleCreationMode = mode => { app.state.palette.creationMode = mode; };
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};
  app.commands.syncConfigToServer = async () => {};

  assert.equal(await app.commands.beginPaletteVariant("source"), true);
  assert.deepEqual(app.state.palette.manualSlots, ["green"]);
  assert.deepEqual(app.commands.manualVariantFilamentIds(), ["red", "blue"]);

  assert.equal(app.commands.removeManualFilamentAt(0), true);
  assert.deepEqual(app.state.palette.manualVariantDraft.workingSlots, [null, "blue"]);
  assert.equal(app.commands.addManualFilament("green"), true);
  assert.deepEqual(app.commands.manualVariantFilamentIds(), ["green", "blue"]);
  assert.equal(app.commands.manualVariantHasChanged(), true);

  assert.equal(app.commands.cancelPaletteVariant(), true);
  assert.deepEqual(app.state.palette.manualSlots, ["green"]);
  assert.deepEqual(app.state.palette.deck[0].filament_ids, ["red", "blue"]);
  assert.equal(app.state.palette.manualVariantDraft, null);
});

test("committing a palette variant inserts an independent active card beside its source", async () => {
  const { app } = await harness();
  const events = [];
  app.events.subscribe("palette.variant.started", detail => events.push({ event: "started", detail }));
  app.events.subscribe("palette.deck.updated", detail => events.push({ event: "updated", detail }));
  app.state.session.allFilaments.push({
    filament_id: "green",
    display_name: "Green",
    has_profile: true,
  });
  app.state.palette.enabledFilaments = new Set(["white", "red", "blue", "green"]);
  app.state.palette.deck = [
    { id: "source", name: "Cloud Study", filament_ids: ["red", "blue"], saved: true },
    { id: "later", name: "Later", filament_ids: ["blue"], saved: false },
  ];
  app.state.palette.activeDeckId = "source";
  app.commands.switchTab = () => {};
  app.commands.toggleCreationMode = mode => { app.state.palette.creationMode = mode; };
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};
  app.commands.syncConfigToServer = async () => {};

  await app.commands.beginPaletteVariant("source");
  assert.equal(await app.commands.commitPaletteVariant(), null);
  app.commands.removeManualFilamentAt(0);
  app.commands.addManualFilament("green");
  const variant = await app.commands.commitPaletteVariant();

  assert.ok(variant);
  assert.equal(variant.name, "Cloud Study Variant");
  assert.equal(variant.saved, false);
  assert.equal(variant.gamut, null);
  assert.deepEqual(variant.filament_ids, ["green", "blue"]);
  assert.deepEqual(app.state.palette.deck.map(card => card.id), [
    "source",
    variant.id,
    "later",
  ]);
  assert.deepEqual(app.state.palette.deck[0].filament_ids, ["red", "blue"]);
  assert.equal(app.state.palette.activeDeckId, variant.id);
  assert.equal(app.state.palette.manualVariantDraft, null);
  assert.equal(events[0].event, "started");
  assert.equal(events[0].detail.cardId, "source");
  assert.equal(events.at(-1).event, "updated");
  assert.equal(events.at(-1).detail.action, "added");
  assert.equal(events.at(-1).detail.card.id, variant.id);
  assert.equal(events.at(-1).detail.sourceCardId, "source");
});

test("committing a palette variant leaves Batch Solve membership unchanged", async () => {
  const { app } = await harness();
  app.state.session.allFilaments.push({
    filament_id: "green",
    display_name: "Green",
    has_profile: true,
  });
  app.state.palette.enabledFilaments = new Set(["white", "red", "blue", "green"]);
  app.state.palette.deck = [
    { id: "source", name: "Cloud Study", filament_ids: ["red", "blue"], saved: false },
    { id: "other", name: "Other", filament_ids: ["green"], saved: false },
  ];
  app.state.palette.activeDeckId = "source";
  app.state.solve.solveMode = "batch";
  app.state.solve.batchSelectedDeckIds = new Set(["source", "other"]);
  app.commands.switchTab = () => {};
  app.commands.toggleCreationMode = mode => { app.state.palette.creationMode = mode; };
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};
  app.commands.syncConfigToServer = async () => {};

  await app.commands.beginPaletteVariant("source");
  app.commands.removeManualFilamentAt(1);
  app.commands.addManualFilament("green");
  const variant = await app.commands.commitPaletteVariant();

  assert.ok(variant);
  assert.deepEqual([...app.state.solve.batchSelectedDeckIds], ["source", "other"]);
  assert.equal(app.state.solve.batchSelectedDeckIds.has(variant.id), false);
  assert.equal(app.state.palette.activeDeckId, "source");
});

test("deck-selected batch start sends mixed ordered palettes without suggestion fields", async () => {
  let payload = null;
  let toast = "";
  const status = {
    job_id: "batch-a",
    job_kind: "palette_batch",
    status: "running",
    phase: "preparing_source",
    items: [
      { position: 1, result_id: "result-1", deck_card_id: "manual", label: "Manual", palette: ["red"], status: "queued" },
      { position: 2, result_id: "result-2", deck_card_id: "saved", label: "Saved", palette: ["red", "blue"], status: "queued" },
    ],
  };
  const { app } = await harness({
    api: {
      getExportStatus: async () => ({ status: "idle" }),
      apiPost: async () => ({ valid: true }),
      startPaletteBatch: async body => { payload = body; return status; },
    },
  });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "image.png", source_ref: null, width: 100, height: 100 };
  app.state.palette.enabledFilaments = new Set(["red", "blue", "white"]);
  app.state.palette.deck = [
    { id: "manual", name: "Manual", filament_ids: ["red"] },
    { id: "saved", name: "Saved", filament_ids: ["red", "blue"] },
  ];
  app.state.solve.batchSelectedDeckIds = new Set(["manual", "saved"]);
  app.state.solve.solveMode = "batch";
  app.commands.appConfirm = async () => true;
  app.commands.syncConfigToServer = async () => {};
  app.commands.syncSolveDimensionsWithGridRemediation = async () => ({ proceed: true, corrected: false });
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.paletteGatingIssueCount = () => 0;
  app.commands.buildSolveRecipeContext = palette => ({
    profile_ref: { kind: "system", id: "system-default" },
    profile_name_at_solve: "Default",
    is_profile_modified_at_solve: false,
    recipe_snapshot: { palette: [...palette] },
  });
  app.commands._currentSettingsSnapshot = () => ({});
  app.commands.ensureBatchPreviewRuns = () => {};
  app.commands.startPaletteBatchPolling = () => {};
  app.commands.renderDeckCards = () => {};
  app.commands.renderSolveProgress = () => {};
  app.commands.switchTab = () => {};
  app.commands.updateSolveReadiness = () => {};
  app.commands.showToast = message => { toast = message; };

  await app.commands.handleStartPaletteBatch();

  assert.ok(payload, toast || "batch start did not call the API");
  assert.deepEqual(payload.deck_palettes, [
    { deck_card_id: "manual", deck_card_name: "Manual", filament_ids: ["red"] },
    { deck_card_id: "saved", deck_card_name: "Saved", filament_ids: ["red", "blue"] },
  ]);
  assert.equal("top_k" in payload, false);
  assert.equal("filament_ids" in payload, false);
  assert.equal("palette_mode" in payload, false);
  assert.equal(app.state.solve.activeSolveJobId, "batch-a");
  assert.equal(app.state.solve.batchDeckLocked, true);
});

test("an accepted batch stays locked and tracked if Preview initialization fails", async () => {
  const status = {
    job_id: "batch-accepted",
    job_kind: "palette_batch",
    status: "running",
    phase: "preparing_source",
    items: [
      { position: 1, result_id: "result-1", deck_card_id: "deck-1", label: "One", palette: ["red"], status: "queued" },
      { position: 2, result_id: "result-2", deck_card_id: "deck-2", label: "Two", palette: ["blue"], status: "queued" },
    ],
  };
  const { app } = await harness({
    api: {
      getExportStatus: async () => ({ status: "idle" }),
      apiPost: async () => ({ valid: true }),
      startPaletteBatch: async () => status,
    },
  });
  let pollingStatus = null;
  let toast = "";
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "image.png", source_ref: null, width: 100, height: 100 };
  app.state.palette.enabledFilaments = new Set(["red", "blue", "white"]);
  app.state.palette.deck = [
    { id: "deck-1", name: "One", filament_ids: ["red"] },
    { id: "deck-2", name: "Two", filament_ids: ["blue"] },
  ];
  app.state.solve.batchSelectedDeckIds = new Set(["deck-1", "deck-2"]);
  app.state.solve.solveMode = "batch";
  app.commands.appConfirm = async () => true;
  app.commands.syncConfigToServer = async () => {};
  app.commands.syncSolveDimensionsWithGridRemediation = async () => ({ proceed: true, corrected: false });
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.paletteGatingIssueCount = () => 0;
  app.commands.buildSolveRecipeContext = () => ({ recipe_snapshot: {} });
  app.commands._currentSettingsSnapshot = () => ({});
  app.commands.ensureBatchPreviewRuns = () => { throw new Error("render failed"); };
  app.commands.startPaletteBatchPolling = value => { pollingStatus = value; };
  app.commands.renderDeckCards = () => {};
  app.commands.updateSolveReadiness = () => {};
  app.commands.showToast = message => { toast = message; };

  await app.commands.handleStartPaletteBatch();

  assert.equal(app.state.solve.activeSolveJobId, "batch-accepted");
  assert.equal(app.state.solve.batchDeckLocked, true);
  assert.equal(pollingStatus, status);
  assert.match(toast, /started, but Preview could not initialize/);
});

test("batch confirmation cancellation performs no work and preserves selection", async () => {
  let starts = 0;
  const events = [];
  const { app } = await harness({
    api: {
      getExportStatus: async () => ({ status: "idle" }),
      apiPost: async () => {
        events.push("palette validation");
        return { valid: true };
      },
      startPaletteBatch: async () => {
        starts += 1;
        return {};
      },
    },
  });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "image.png", source_ref: null };
  app.state.palette.deck = [
    { id: "deck-1", name: "One", filament_ids: ["red"] },
    { id: "deck-2", name: "Two", filament_ids: ["blue"] },
  ];
  app.state.solve.batchSelectedDeckIds = new Set(["deck-1", "deck-2"]);
  app.state.solve.solveMode = "batch";
  app.commands.syncSolveSettingsWithPitchRemediation = async () => {
    events.push("settings preflight");
    return { proceed: true, corrected: false };
  };
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.syncSolveDimensionsWithGridRemediation = async () => {
    events.push("dimension preflight");
    return { proceed: true, corrected: false };
  };
  app.commands.appConfirm = async () => {
    events.push("batch confirmation");
    return false;
  };
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.paletteGatingIssueCount = () => 0;

  await app.commands.handleStartPaletteBatch();

  assert.deepEqual(events, [
    "settings preflight",
    "palette validation",
    "palette validation",
    "dimension preflight",
    "batch confirmation",
  ]);
  assert.equal(starts, 0);
  assert.equal(app.state.solve.paletteBatchStartPending, false);
  assert.equal(app.state.solve.batchDeckLocked, false);
  assert.equal(app.state.solve.solveMode, "batch");
  assert.deepEqual([...app.state.solve.batchSelectedDeckIds], ["deck-1", "deck-2"]);
});

test("a rejected batch start unlocks the deck without discarding mode or selection", async () => {
  const { app } = await harness({
    api: {
      getExportStatus: async () => ({ status: "idle" }),
      apiPost: async () => ({ valid: true }),
      startPaletteBatch: async () => { throw new Error("server rejected batch"); },
    },
  });
  let toast = "";
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "image.png", source_ref: null };
  app.state.palette.deck = [
    { id: "deck-1", name: "One", filament_ids: ["red"] },
    { id: "deck-2", name: "Two", filament_ids: ["blue"] },
  ];
  app.state.solve.batchSelectedDeckIds = new Set(["deck-1", "deck-2"]);
  app.state.solve.solveMode = "batch";
  app.commands.appConfirm = async () => true;
  app.commands.syncConfigToServer = async () => {};
  app.commands.syncSolveDimensionsWithGridRemediation = async () => ({ proceed: true, corrected: false });
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.paletteGatingIssueCount = () => 0;
  app.commands.buildSolveRecipeContext = () => ({ recipe_snapshot: {} });
  app.commands._currentSettingsSnapshot = () => ({});
  app.commands.renderDeckCards = () => {};
  app.commands.updateSolveReadiness = () => {};
  app.commands.showToast = message => { toast = message; };

  await app.commands.handleStartPaletteBatch();

  assert.equal(app.state.solve.batchDeckLocked, false);
  assert.equal(app.state.solve.solveMode, "batch");
  assert.deepEqual([...app.state.solve.batchSelectedDeckIds], ["deck-1", "deck-2"]);
  assert.match(toast, /server rejected batch/);
});

test("palette batch results fetch once and cancellation removes only incomplete runs", async () => {
  let fetches = 0;
  const { app } = await harness({
    api: {
      getPaletteBatchResult: async (_jobId, resultId) => {
        fetches += 1;
        return {
          result_id: resultId,
          label: "Manual",
          palette: ["red"],
          config: { palette: ["red"], base_filament: "white" },
          recipe_snapshot: { palette: ["red"] },
          result: { card_id: resultId, mean_de: 0.1 },
          elapsed_s: 2,
        };
      },
    },
  });
  app.commands.renderSolveTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};
  app.state.solve.solveRuns = [
    { id: "result-1", label: "queued", palette: ["red"], results: null },
    { id: "result-2", label: "queued", palette: ["blue"], results: null },
  ];

  const running = {
    job_id: "batch-a",
    job_kind: "palette_batch",
    status: "running",
    items: [
      { result_id: "result-1", deck_card_id: "deck-1", position: 1, palette: ["red"], status: "complete", result_available: true },
      { result_id: "result-2", deck_card_id: "deck-2", position: 2, palette: ["blue"], status: "queued", result_available: false },
    ],
  };
  await app.commands.reconcilePaletteBatchStatus(running, { awaitResults: true });
  await app.commands.reconcilePaletteBatchStatus(running, { awaitResults: true });
  assert.equal(fetches, 1);
  assert.equal(app.state.solve.solveRuns[0].results.card_id, "result-1");

  await app.commands.reconcilePaletteBatchStatus({
    ...running,
    status: "cancelled",
    items: [running.items[0], { ...running.items[1], status: "cancelled" }],
  });
  assert.deepEqual(app.state.solve.solveRuns.map(run => run.id), ["result-1"]);
});

test("selected source cards and whole-deck clearing are locked during a batch", async () => {
  const { app } = await harness();
  let toast = "";
  app.commands.showToast = message => { toast = message; };
  app.commands.renderDeckCards = () => {};
  app.commands.updateRail = () => {};
  app.commands.syncConfigToServer = () => {};
  app.state.palette.deck = [
    { id: "locked", name: "Locked", filament_ids: ["red"] },
    { id: "free", name: "Free", filament_ids: ["blue"] },
  ];
  app.state.solve.batchDeckLocked = true;
  app.state.solve.batchLockedDeckIds = new Set(["locked"]);

  assert.equal(await app.commands.removeDeckCard("locked"), false);
  assert.match(toast, /locked/i);
  assert.deepEqual(app.state.palette.deck.map(card => card.id), ["locked", "free"]);
  assert.equal(await app.commands.removeDeckCard("free"), true);
  assert.deepEqual(app.state.palette.deck.map(card => card.id), ["locked"]);
});
test("palette hover preview omits the internal out-of-gamut diagnostic", async () => {
  const { app } = await harness();
  app.commands.esc = (value) => String(value);
  const html = app.commands.buildRailDeckHoverPreview({
    name: "Preview palette",
    filament_ids: ["red"],
    gamut: {
      status: "done",
      suggestion_mean_de: 0.024,
      n_out_of_gamut: 37,
      total_pixels: 100,
    },
  });

  assert.match(html, /Suggest dE 0\.024/);
  assert.doesNotMatch(html, /\bOOG\b|>37</);
});

test("suggestion clear confirmation cancels stale timers and synchronizes accessibility", async () => {
  const clearButton = fakeElement();
  const { app } = await harness({ elements: { "#clearDeckBtn": clearButton } });
  app.commands.renderCreationTab = () => {};
  app.state.palette.stagingDeck = [{ id: "suggest-1" }];

  app.commands.syncStagingClearButton();
  assert.equal(clearButton.disabled, false);
  assert.equal(clearButton.textContent, "Clear");
  assert.equal(clearButton["aria-label"], "Clear suggested palettes");

  app.commands.armStagingClearConfirm();
  assert.equal(clearButton.textContent, "Confirm?");
  assert.equal(clearButton["aria-label"], "Confirm clearing all suggested palettes");
  assert.ok(app.state.palette.stagingClearConfirmTimer);

  app.commands.handleStagingClearClick();
  assert.deepEqual(app.state.palette.stagingDeck, []);
  assert.equal(app.state.palette.stagingClearConfirmTimer, null);
  assert.equal(app.state.palette.stagingClearConfirmPending, false);
  assert.equal(clearButton.disabled, true);
  assert.equal(clearButton.textContent, "Clear");
  assert.equal(clearButton.title, "No suggested palettes to clear");
});

test("runtime-scoped filament persistence never falls back to ambient legacy identity", async () => {
  const storage = memoryStorage({ prisma_enabled_filaments: '["red"]' });
  const { app } = await harness({ storage });
  assert.equal(app.commands.authoritativeRuntimeLibraryId(), null);
  assert.equal(app.state.palette.enabledFilamentPersistenceReady, false);
  app.state.session.modelLibraryManager.status = { runtime_active_library_id: "library-a" };
  assert.equal(app.commands.authoritativeRuntimeLibraryId(), "library-a");
  app.commands.reconcileEnabledFilamentsForRuntimeLibrary();
  const scoped = JSON.parse(storage.getItem("prisma_enabled_filaments_by_library"));
  assert.ok(scoped.libraries["library-a"]);
  assert.equal(storage.getItem("prisma_enabled_filaments"), null);
});

test("loaded palette restoration prefers deck, then saved library, then ad hoc", async () => {
  const { app } = await harness();
  const support = { base: "white", capEffective: "white" };
  const deck = [{ id: "deck-a", filament_ids: ["red", "blue"], support }];
  assert.deepEqual(app.commands.chooseLoadedPaletteRestoreAction({
    filamentIds: ["red", "blue"], support, deckCards: deck, savedPalettes: [],
  }), { kind: "reuse-deck", cardId: "deck-a" });
  assert.equal(app.commands.chooseLoadedPaletteRestoreAction({
    filamentIds: ["red"], support, deckCards: [], savedPalettes: [],
  }).kind, "add-ad-hoc");
});

test("solve runs own isolated immutable export histories", async () => {
  const { app } = await harness();
  const first = { id: "run-a", palette: ["red"], config: { t_max: 2 } };
  const second = { id: "run-b", palette: ["blue"], config: { t_max: 3 } };
  app.commands.ensureSolveRunExportState(first);
  app.commands.ensureSolveRunExportState(second);
  const result = {
    export_id: "export-a", output_format: "3mf", geometry_source: "field_derived",
    field_scale: 4, swap_plan: { instructions: "None" }, files: [],
  };
  const record = app.commands.appendExportRecordToRun(first, result, 100, 2.5);
  result.output_format = "stl";
  assert.equal(record.outputFormat, "3mf");
  assert.equal(first.exportRecords.length, 1);
  assert.equal(second.exportRecords.length, 0);
  assert.equal(app.commands.getSelectedExportRecord(first).id, "export-a");
});

test("export preview and dimensions use solved appearance and frozen border settings", async () => {
  const { app } = await harness();
  const run = {
    config: { border: true, border_width_mm: 2 },
    results: {
      predicted_appearance_url: "/appearance.png", predicted_url: "/predicted.png",
      image_w: 60, image_h: 40, image_domain_width_mm: 24, image_domain_height_mm: 16,
    },
  };
  assert.equal(app.commands.getExportSolvePreviewUrl(run), "/appearance.png");
  assert.deepEqual(app.commands.getExportSolveDimensions(run), {
    imageWidthPx: 60, imageHeightPx: 40,
    imageWidthMm: 24, imageHeightMm: 16,
    totalWidthMm: 28, totalHeightMm: 20,
    borderEnabled: true, borderWidthMm: 2,
  });
});

test("white-cap thickness semantics keep stable total, boundary, and detail slots", async () => {
  const { app } = await harness();
  const items = app.commands.getSolveWhiteCapThicknessItems({ results: {
    cap_map_url: "/total.png", detail_cap_map_url: "/detail.png",
  } });
  assert.deepEqual(items.map((item) => item.key), ["cap:total", "cap:boundary", "cap:detail"]);
  assert.deepEqual(items.map((item) => item.available), [true, false, true]);
  assert.deepEqual(app.commands.getSolveThicknessItems({ results: {
    filament_maps: [], cap_map_url: "/total.png", detail_cap_map_url: "/detail.png",
  } }).map((item) => item.key), ["cap:total", "cap:detail"]);
});

test("card lightbox dispatcher routes semantic card kinds through explicit commands", async () => {
  const { app } = await harness();
  const calls = [];
  app.commands.openSolveRunLightbox = (...args) => calls.push(["run", ...args]);
  app.commands.openSurfaceLightbox = (...args) => calls.push(["surface", ...args]);
  app.commands.openRecipeLightbox = (...args) => calls.push(["recipe", ...args]);
  app.commands.openThicknessLightboxForKey = (...args) => calls.push(["thickness", ...args]);
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "run", runId: "run-a", view: "predicted",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "surface", runId: "run-a", view: "surface_explorer",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "recipe", runId: "run-a",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "thickness", runId: "run-a", mapKey: "cap:detail",
  } });
  assert.deepEqual(calls, [
    ["run", "run-a", "predicted"],
    ["surface", "surface_explorer", "run-a"],
    ["recipe", "run-a"],
    ["thickness", "run-a", "cap:detail"],
  ]);
});

test("active solve and export ownership block destructive run deletion", async () => {
  const { app } = await harness();
  const run = { id: "run-a", results: null };
  app.state.solve.activeSolveRunId = run.id;
  app.state.solve.solveStatus = { status: "running" };
  assert.match(app.commands.getSolveRunDeleteBlockReason(run), /Cancel this solve/);
  run.results = {};
  app.state.solve.solveStatus = { status: "idle" };
  app.state.export.exportRunning = true;
  app.state.export.activeExportRunId = run.id;
  assert.match(app.commands.getSolveRunDeleteBlockReason(run), /Cancel this export/);
});

test("image aspect default uses 120 mm on the source short side", async () => {
  const { app } = await harness();
  app.state.image.selectedImage = { width: 800, height: 400 };
  app.commands.applyImageAspectDefault();
  assert.equal(app.state.image.frameState.heightMm, 120);
  assert.equal(app.state.image.frameState.widthMm, 240);
  app.state.image.selectedImage = { width: 400, height: 800 };
  app.commands.applyImageAspectDefault();
  assert.equal(app.state.image.frameState.widthMm, 120);
  assert.equal(app.state.image.frameState.heightMm, 240);
});

test("saved-run identity, timestamp, and tiers are stable behavior", async () => {
  const { app } = await harness();
  const saved = { save_id: "save-a", tier: "saved", saved_at: "2026-07-16T12:00:00Z" };
  assert.equal(app.commands.savedRunKey(saved), "saved:save-a");
  assert.equal(app.commands.savedRunTierLabel(saved), "Saved");
  assert.match(app.commands.formatSavedRunTimestamp(saved.saved_at), /2026|Jul/);
});

test("run name validation reserves automatic labels and clears automatic save defaults", async () => {
  const { app } = await harness();
  for (const label of ["Run 7", " run 007 ", "rUn 9"]) {
    assert.match(app.commands.validateWritableRunLabel(label), /reserved for automatic run labels/);
  }
  assert.equal(app.commands.validateWritableRunLabel("Run Seven"), "");
  assert.equal(app.commands.validateWritableRunLabel(""), "Run name cannot be empty.");
  assert.equal(app.commands.initialSaveRunLabel({ label: "Run 42" }), "");
  assert.equal(app.commands.initialSaveRunLabel({ label: "Portrait" }), "Portrait");
});

test("generic numeric module controls clamp the flattening cap to descriptor bounds", async () => {
  const { app } = await harness();
  const descriptor = { type: "int", min: 2, max: 500 };
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "1", 100), {
    ok: true, value: 2,
  });
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "501", 100), {
    ok: true, value: 500,
  });
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "2", 100), {
    ok: true, value: 2,
  });
});

test("saving a run is single-flight and applies the server label authoritatively", async () => {
  let releaseSave;
  let saveCalls = 0;
  const saveResponse = new Promise((resolve) => { releaseSave = resolve; });
  const button = fakeElement();
  const { app } = await harness({ api: {
    saveRun: async () => { saveCalls += 1; return saveResponse; },
  } });
  const run = { id: "run-a", label: "Run 1", results: {} };
  app.state.solve.solveRuns = [run];
  let solveRenders = 0;
  app.commands.renderSolveTab = () => { solveRenders += 1; };
  app.commands.showToast = () => {};

  const first = app.commands.saveSolveRun(run.id, "Portrait", button);
  const duplicate = await app.commands.saveSolveRun(run.id, "Duplicate", button);
  assert.equal(duplicate, false);
  assert.equal(saveCalls, 1);
  assert.equal(button.disabled, true);
  assert.equal(button["aria-busy"], "true");

  releaseSave({ save_id: "stable-save-id", label: "Server Portrait" });
  assert.equal(await first, true);
  assert.equal(run.label, "Server Portrait");
  assert.equal(run.save_pending, false);
  assert.equal(solveRenders, 1);
});

test("failed or orphaned saves do not mutate unrelated run state", async () => {
  const button = fakeElement();
  const { app } = await harness({ api: {
    saveRun: async () => { throw new Error("save failed"); },
  } });
  const run = { id: "run-a", label: "Original", results: {} };
  app.state.solve.solveRuns = [run];
  app.commands.showToast = () => {};
  app.commands.renderSolveTab = () => {};
  assert.equal(await app.commands.saveSolveRun(run.id, "New name", button), false);
  assert.equal(run.label, "Original");
  assert.equal(run.save_pending, false);
  assert.equal(button.disabled, false);
  assert.equal(button["aria-busy"], undefined);

  let releaseSave;
  app.api.saveRun = () => new Promise((resolve) => { releaseSave = resolve; });
  let renderCount = 0;
  app.commands.renderSolveTab = () => { renderCount += 1; };
  const orphaned = app.commands.saveSolveRun(run.id, "Orphan", button);
  app.state.solve.solveRuns = [];
  releaseSave({ save_id: "save-a", label: "Server Orphan" });
  assert.equal(await orphaned, true);
  assert.equal(renderCount, 0);
});

test("authoritative run labels refresh open lightbox and settings surfaces", async () => {
  const { app } = await harness();
  const title = fakeElement();
  title.textContent = "Run 1";
  const contour = fakeElement();
  contour.setAttribute("aria-label", "Run 1 layer contours");
  const lightboxContent = fakeElement();
  lightboxContent.querySelector = (selector) => (
    selector === ".comp-lightbox-runtitle" ? title : null
  );
  lightboxContent.querySelectorAll = (selector) => (
    selector === "[aria-label]" ? [contour] : []
  );
  app.state.ui.$ = (selector) => (
    selector === "#compLightboxContent" ? lightboxContent : null
  );
  app.state.solve._solveLightboxState = { runId: "run-a", kind: "solve" };
  app.commands.refreshOpenSolveRunLabels(
    { id: "run-a", label: "Server Portrait" },
    "Run 1",
  );
  assert.equal(title.textContent, "Server Portrait");
  assert.equal(contour["aria-label"], "Server Portrait layer contours");

  const settingsLabel = fakeElement();
  const settingsToggle = fakeElement();
  const settingsBody = fakeElement();
  const settingsPanel = fakeElement();
  settingsPanel.querySelector = (selector) => ({
    ".run-settings-run-label": settingsLabel,
    ".run-settings-advanced-toggle": settingsToggle,
    ".run-settings-body": settingsBody,
  })[selector] || null;
  app.state.solve.solveRuns = [{ id: "run-a", label: "Server Portrait" }];
  app.state.solve.solveRunSettingsPanelEl = settingsPanel;
  app.state.solve.solveRunSettingsPanelRunId = "run-a";
  app.commands.buildReadOnlyRunSettingsHtml = () => "settings";
  app.commands.renderSolveRunSettingsPanel();
  assert.equal(settingsPanel["aria-label"], "Settings used by Server Portrait");
  assert.equal(settingsLabel.textContent, "Server Portrait");
  assert.equal(settingsToggle.classList.contains("is-active"), false);
  app.state.solve.solveRunSettingsAdvancedVisible = true;
  app.commands.renderSolveRunSettingsPanel();
  assert.equal(settingsToggle.classList.contains("is-active"), true);
});

test("saved-run review distinguishes canonical defaults from unavailable values", async () => {
  const { app } = await harness();
  const run = { label: "Sparse archive", config: {} };
  const frozen = app.commands.getFrozenSolveRunSnapshot(run);
  const essentials = app.state.ui.SETTINGS_PRESENTATION.find((section) => section.key === "essentials");
  const rows = app.commands.buildReadOnlyRunSectionRows(essentials, frozen);
  assert.equal(rows.find((row) => row.label === "Max Total Thickness").value, "Default: 3 mm");
  assert.equal(
    app.commands.formatReadOnlyRunSetting({ key: "historical_unknown" }, undefined, {}),
    "Unavailable in saved run",
  );
});

test("saved-run review uses current preprocessing modules only", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [];
  const run = {
    label: "Current module run",
    config: { preprocessing_params: { retired_filter: { strength: 0.4 } } },
    results: {
      solve_start_diagnostics: {
        module_state: { retired_filter: true },
        module_settings: { retired_filter: { strength: 0.4 } },
      },
    },
  };
  const frozen = app.commands.getFrozenSolveRunSnapshot(run);
  assert.equal(frozen.activePreprocessing.has("retired_filter"), false);
  const section = app.state.ui.SETTINGS_PRESENTATION.find((entry) => entry.key === "preprocessing");
  const rows = app.commands.buildReadOnlyRunSectionRows(section, frozen);
  assert.equal(rows.some((row) => row.label === "Retired Filter"), false);
});

test("saved-run review labels missing module parameters as defaults", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [{
    name: "demo_filter",
    slot: "preprocessing",
    params: {
      strength: { name: "strength", label: "Strength", type: "number", unit: "mm", default: 0.4 },
    },
  }];
  const section = app.state.ui.SETTINGS_PRESENTATION.find((entry) => entry.key === "preprocessing");
  const rows = app.commands.buildReadOnlyRunSectionRows(section, {
    settings: { preprocessing_params: {} },
    activePreprocessing: new Set(["demo_filter"]),
    preprocessingStateKnown: true,
  });
  assert.equal(rows.find((row) => row.label === "Strength").value, "Default: 0.4 mm");
});

test("saved-run review keeps module enablement in the main preprocessing block", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [
    { name: "demo_filter", slot: "preprocessing", params: {} },
  ];
  const section = app.state.ui.SETTINGS_PRESENTATION.find((entry) => entry.key === "preprocessing");
  const rows = app.commands.buildReadOnlyRunSectionRows(section, {
    settings: {},
    activePreprocessing: new Set(),
    preprocessingStateKnown: true,
  });
  assert.equal(rows.find((row) => row.controlId === "module:demo_filter").group, "");
});

test("settings presentation rows point at real controls and omit retired fields", async () => {
  const { app } = await harness();
  const html = fs.readFileSync(
    path.join(__dirname, "../../Prisma/generator/app/index.html"),
    "utf8",
  );
  const rows = app.state.ui.SETTINGS_PRESENTATION.flatMap((section) => section.rows);
  assert.ok(rows.length > 0);
  for (const row of rows) {
    assert.ok(row.controlId, `missing control id for ${row.key}`);
    assert.match(html, new RegExp(`id=["']${row.controlId}["']`));
  }
  assert.equal(app.state.ui.SETTINGS_PRESENTATION, app.state.ui.READ_ONLY_RUN_SETTING_SECTIONS);
  assert.doesNotMatch(html, /cfgUseCorrections|cfgStage2BoundaryMutationPercentile|cfgStage2BoundaryMutationMinComponent/);
  assert.ok(!rows.some((row) => [
    "use_corrections",
    "stage2_boundary_mutation_current_de_percentile",
    "stage2_boundary_mutation_min_component_mm",
  ].includes(row.key)));

  const settingsCss = fs.readFileSync(
    path.join(__dirname, "../../Prisma/generator/app/styles/settings.css"),
    "utf8",
  );
  const solveCss = fs.readFileSync(
    path.join(__dirname, "../../Prisma/generator/app/styles/solve.css"),
    "utf8",
  );
  assert.match(settingsCss, /text-align-last:\s*left/);
  assert.match(settingsCss, /-webkit-appearance:\s*textfield/);
  assert.match(settingsCss, /input\[type="number"\]::\-webkit-inner-spin-button/);
  assert.match(settingsCss, /\.input-with-unit\.settings-has-steppers[\s\S]*width:\s*max-content/);
  assert.match(settingsCss, /\.settings-subsection-head[\s\S]*border-top:\s*1px solid var\(--line\)[\s\S]*border-bottom:\s*1px solid var\(--line\)/);
  assert.match(solveCss, /\.run-settings-subsection-cap[\s\S]*border-top:\s*1px solid var\(--line\)[\s\S]*border-bottom:\s*1px solid var\(--line\)/);
  assert.ok(settingsCss.includes(".settings-derived-footnote"));
  assert.match(html, /style\.css\?v=2026-08-04-settings-polish-v3/);
  for (const controlId of ["cfgDWcMin", "cfgBaseShadingLimit", "cfgDetailCapMaxLayers"]) {
    assert.match(html, new RegExp(`<input type="text" id="${controlId}"`));
  }
  assert.ok(html.indexOf('id="stackDerived"') < html.indexOf('id="derivedParams"'));
});

test("solve-history clear controls stay synchronized and disable invalid actions", async () => {
  const solveClear = fakeElement();
  const exportClear = fakeElement();
  const { app } = await harness({ elements: {
    "#clearSolveHistoryBtn": solveClear,
    "#exportClearSolveHistoryBtn": exportClear,
  } });
  app.state.solve.solveRuns = [{ id: "run-a", results: {} }];
  app.state.solve.solveStatus = { status: "idle" };
  app.state.export.exportRunning = false;

  app.commands.syncSolveHistoryClearButtons();
  assert.equal(solveClear.disabled, false);
  assert.equal(exportClear.disabled, false);
  app.commands.armSolveHistoryClearConfirm();
  assert.equal(solveClear.textContent, "Clear?");
  assert.equal(exportClear.textContent, "Clear?");
  assert.equal(solveClear["aria-label"], "Confirm clearing all solve runs");
  app.commands.resetSolveHistoryClearConfirm();
  assert.equal(solveClear.textContent, "Clear");
  assert.equal(exportClear.textContent, "Clear");

  app.state.solve.solveRuns = [];
  app.commands.syncSolveHistoryClearButtons();
  assert.equal(solveClear.disabled, true);
  assert.equal(exportClear.disabled, true);
  assert.equal(solveClear.title, "No solve runs to clear");
});

test("solve cards expose listbox semantics, omit loaded provenance, and gate Save by completion", async () => {
  const cards = fakeElement();
  const { app } = await harness({ elements: { "#solveRunCards": cards } });
  app.commands.hideSolveRunHoverPreview = () => {};
  app.commands.bindSolveRunCardAuxiliaryInteractions = () => {};
  app.commands.formatSolveRunCardRmse = () => "1.2 dE";
  app.commands.esc = (value) => String(value);
  app.commands.escAttr = (value) => String(value);
  app.state.solve.solveRuns = [{
    id: "run-a", label: "Loaded portrait", loaded_from_archive: true,
    palette: ["red"], results: null,
    config: { base_filament: "white", cap_filament: "__same__" },
  }];

  app.commands.renderSolveRunSidebar();

  assert.equal(cards.role, "listbox");
  assert.equal(cards["aria-multiselectable"], "true");
  assert.match(cards.innerHTML, /role="option"/);
  assert.match(cards.innerHTML, /aria-selected="false"/);
  assert.match(cards.innerHTML, /solve-run-save-btn[^>]+disabled/);
  assert.match(cards.innerHTML, /solve-run-delete-slot/);
  assert.doesNotMatch(cards.innerHTML, />Confirm\?</);
  assert.match(cards.innerHTML, /solve-run-support-tray/);
  assert.match(cards.innerHTML, /White Base\/Cap: White/);
  assert.doesNotMatch(cards.innerHTML, />Loaded<\/span>|solve-run-loaded-badge/);
});

test("image library refresh preserves a private saved-run source outside available images", async () => {
  const privateSource = {
    filename: "portrait.jpg",
    width: 1200,
    height: 800,
    source_ref: "loaded-run:loaded-1",
    temporary: true,
  };
  const { app } = await harness({ api: {
    fetchImages: async () => [{ filename: "library.jpg", width: 640, height: 480 }],
  } });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = privateSource;
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => { syncCount += 1; };

  await app.commands.refreshImageLibrary();

  assert.equal(app.state.image.selectedImage, privateSource);
  assert.deepEqual(app.state.image.availableImages.map(image => image.filename), ["library.jpg"]);
  assert.equal(syncCount, 0);
});

test("config synchronization carries the active private source reference and clears it for library images", async () => {
  const payloads = [];
  const { app } = await harness({ api: {
    updateConfig: async (payload) => {
      payloads.push(payload);
      return { config: payload };
    },
  } });
  app.state.session.apiConnected = true;
  app.state.session.printerConfig = { ams_slots: 4, white_slots: 1 };
  app.commands.syncConfigFromModuleState = () => {};
  app.commands.readConfigFromUI = () => {};
  app.commands.getActivePalette = () => ["red"];
  app.commands.getBaseFilament = () => "white";
  app.state.image.selectedImage = {
    filename: "portrait.jpg",
    source_ref: "loaded-run:loaded-1",
  };

  await app.commands.syncConfigToServer({ throwOnError: true });
  app.state.image.selectedImage = { filename: "library.jpg" };
  await app.commands.syncConfigToServer({ throwOnError: true });

  assert.equal(payloads[0].image_path, "portrait.jpg");
  assert.equal(payloads[0].image_source_ref, "loaded-run:loaded-1");
  assert.equal(payloads[1].image_path, "library.jpg");
  assert.equal(payloads[1].image_source_ref, null);
});

test("failed loaded-run synchronization rolls back all staged client state", async () => {
  const { app } = await harness({ api: {
    fetchImages: async () => [],
  } });
  const previousImage = { filename: "library.jpg" };
  const previousDeck = [{ id: "deck-1", filament_ids: ["blue"] }];
  app.state.image.selectedImage = previousImage;
  app.state.settings.config.t_max = 3;
  app.state.palette.deck = previousDeck;
  app.state.palette.activeDeckId = "deck-1";
  app.commands.loadImages = async () => {};
  app.commands.applyImageAspectDefault = () => {};
  app.commands.renderSettingsTab = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderExportTab = () => {};
  app.commands.renderFrameCanvas = () => {};
  app.commands.updateRail = () => {};
  app.commands.restoreLoadedRunPaletteToDeck = async () => {
    app.state.palette.deck = [{ id: "loaded-deck", filament_ids: ["red"] }];
    app.state.palette.activeDeckId = "loaded-deck";
  };
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => {
    syncCount += 1;
    if (syncCount === 1) throw new Error("network interrupted");
  };

  await assert.rejects(
    app.commands.applyLoadedRun({
      card_id: "loaded-1",
      label: "Portrait",
      config: {
        image_path: "portrait.jpg",
        image_source_ref: "loaded-run:loaded-1",
        palette: ["red"],
        white_base: "white",
        t_max: 9,
      },
      source_image: {
        filename: "portrait.jpg",
        source_ref: "loaded-run:loaded-1",
        temporary: true,
      },
      result: {},
    }),
    /network interrupted/,
  );

  assert.equal(app.state.solve.solveRuns.length, 0);
  assert.equal(app.state.solve.solveRunCounter, 0);
  assert.equal(app.state.solve.loadedRunApplyRunning, false);
  assert.equal(app.state.image.selectedImage, previousImage);
  assert.equal(app.state.settings.config.t_max, 3);
  assert.deepEqual(app.state.palette.deck, previousDeck);
  assert.equal(app.state.palette.activeDeckId, "deck-1");
  assert.equal(syncCount, 2);
});

test("loaded result keeps historical printability while re-solve state uses active printer", async () => {
  const { app } = await harness();
  app.state.session.activePrintability = {
    minimum_extrusion_width_mm: 0.4,
    minimum_line_length_mm: 0.8,
    minimum_component_area_mm2: 0.32,
  };
  app.commands.renderSettingsTab = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderFrameCanvas = () => {};
  app.commands.updateRail = () => {};
  app.commands.restoreLoadedRunPaletteToDeck = async () => {};
  app.commands.syncConfigToServer = async () => {
    app.state.settings.config.printability_minimum_extrusion_width_mm =
      app.state.session.activePrintability.minimum_extrusion_width_mm;
    app.state.settings.config.printability_minimum_line_length_mm =
      app.state.session.activePrintability.minimum_line_length_mm;
  };

  await app.commands.applyLoadedRun({
    card_id: "loaded-1",
    label: "Historical run",
    config: {
      palette: ["red"],
      white_base: "white",
      printability_minimum_extrusion_width_mm: 0.16,
      printability_minimum_line_length_mm: 0.4,
    },
    result: {},
  });

  const loadedCard = app.state.solve.solveRuns[0];
  assert.equal(loadedCard.config.printability_minimum_extrusion_width_mm, 0.16);
  assert.equal(loadedCard.config.printability_minimum_line_length_mm, 0.4);
  assert.equal(app.state.settings.config.printability_minimum_extrusion_width_mm, 0.4);
  assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.8);
});

test("Clear Temp drains config sync and force-reconciles private source and stale histories", async () => {
  let clearCalled = false;
  const { app } = await harness({ api: {
    clearAllTempFiles: async () => {
      clearCalled = true;
      return {
        cleared_source_ref: "loaded-run:loaded-1",
        active_image_cleared: true,
        config: { image_path: null, image_source_ref: null },
      };
    },
  } });
  const eventBindings = await import(moduleUrl("features/event-bindings.js"));
  eventBindings.installFeaturesEventBindings(app);
  const clearButton = fakeElement();
  app.state.ui.$ = (selector) => selector === "#clearAllTempBtn" ? clearButton : null;
  app.commands.appConfirm = async () => true;
  app.commands.showToast = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderExportTab = () => {};
  app.state.image.selectedImage = {
    filename: "portrait.jpg",
    source_ref: "loaded-run:loaded-1",
  };
  app.state.settings.config.image_path = "portrait.jpg";
  app.state.settings.config.image_source_ref = "loaded-run:loaded-1";
  app.state.solve.solveRuns = [{ id: "loaded-1", results: {} }];
  app.state.solve.solveStatus = { status: "running" };
  app.state.export.exportRunning = true;
  let releaseSync;
  app.state.settings._configSyncChain = new Promise((resolve) => {
    releaseSync = resolve;
  });
  app.commands.syncConfigToServer = async () => {};
  const clearing = app.commands.clearAllTempFiles();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(clearCalled, false);
  assert.equal(clearButton.disabled, true);
  releaseSync();
  await clearing;

  assert.equal(clearCalled, true);
  assert.equal(app.state.image.selectedImage, null);
  assert.equal(app.state.solve.solveRuns.length, 0);
  assert.equal(app.state.solve.solveStatus.status, "idle");
  assert.equal(app.state.export.exportRunning, false);
  assert.equal(clearButton.disabled, false);
  assert.equal(app.state.session.clearTempRunning, false);
});

test("operation progress exposes cancellation only to its owning workflow", async () => {
  const overlay = fakeElement(); const cancel = fakeElement(); const fill = fakeElement();
  const label = fakeElement(); const elapsed = fakeElement();
  overlay.querySelector = () => fill;
  const elements = {
    "#dataSourceBadge": fakeElement(), "#tabSwitch": fakeElement(),
    "#settingsDrawer": fakeElement(), "#opProgress": overlay,
    "#compLightbox": fakeElement(), "#opProgressCancel": cancel,
    "#opProgressLabel": label, "#opProgressElapsed": elapsed,
  };
  const { app } = await harness({ elements });
  app.state.ui.$ = (selector) => elements[selector] || null;
  app.commands.startProgress("Generating", "swap-instructions", { cancellable: false });
  assert.equal(overlay.dataset.owner, "swap-instructions");
  assert.equal(overlay.dataset.cancellable, "false");
  assert.equal(cancel.hidden, true);
  assert.equal(cancel.disabled, true);
  app.commands.stopProgress();
});

test("profile loading drains stale sync work before applying and persisting saved values", async () => {
  const { app } = await harness();
  app.state.session.apiConnected = true;
  app.state.settings.config.t_max = 4;
  let releaseStaleSync;
  app.state.settings._configSyncChain = new Promise((resolve) => {
    releaseStaleSync = () => { app.state.settings.config.t_max = 4; resolve(); };
  });
  let syncedValue = null;
  app.commands._applyModuleSnapshot = async () => {};
  app.commands.renderSettingsTab = () => {};
  app.commands.renderSettingsProfileBar = () => {};
  app.commands.syncConfigToServer = async () => { syncedValue = app.state.settings.config.t_max; };

  const loading = app.commands._doLoadSettingsProfile({
    id: "saved", kind: "named", name: "Saved", settings: { t_max: 3 }, modules: {},
  });
  await Promise.resolve();
  releaseStaleSync();
  await loading;
  assert.equal(app.state.settings.config.t_max, 3);
  assert.equal(syncedValue, 3);
  assert.equal(app.state.settings.loadedProfileSnapshot.settings.t_max, 3);
});

test("profile load failures restore prior local state and do not claim clean success", async () => {
  const { app } = await harness();
  app.state.session.apiConnected = true;
  app.state.settings.config.t_max = 4;
  app.commands.renderSettingsTab = () => {};
  app.commands.renderSettingsProfileBar = () => {};
  app.commands._applyModuleSnapshot = async () => {};
  app.commands.syncConfigToServer = async () => { throw new Error("config write failed"); };
  await assert.rejects(
    app.commands._doLoadSettingsProfile({
      id: "broken", kind: "named", name: "Broken", settings: { t_max: 3 }, modules: {},
    }),
    /config write failed/,
  );
  assert.equal(app.state.settings.config.t_max, 4);
  assert.equal(app.state.settings.loadedProfileRef, null);
  assert.equal(app.state.settings.loadedProfileSnapshot, null);
});

test("solve start stops cleanly when solve-pitch remediation is cancelled", async () => {
  let starts = 0;
  let remediationCalls = 0;
  const { app } = await harness({ api: {
    getExportStatus: async () => ({ status: "idle" }),
    startSolve: async () => {
      starts += 1;
      return { job_id: "unexpected" };
    },
  } });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "image.png", source_ref: null };
  app.commands.updateSolveReadiness = () => {};
  app.commands.syncSolveSettingsWithPitchRemediation = async () => {
    remediationCalls += 1;
    return { proceed: false, corrected: false };
  };

  await app.commands.handleStartSolve();

  assert.equal(remediationCalls, 1);
  assert.equal(starts, 0);
  assert.equal(app.state.solve.solveStartPending, false);
  assert.equal(app.state.solve.solveRuns.length, 0);
});

test("solve start uses the injected palette preflight and blocks invalid palettes", async () => {
  const calls = [];
  const { app } = await harness({ api: {
    getExportStatus: async () => ({ status: "idle" }),
    apiPost: async (path, body) => {
      calls.push([path, body]);
      return { valid: false, excluded: ["red"] };
    },
    startSolve: async () => { calls.push(["start"]); return { job_id: "unexpected" }; },
  } });
  app.commands.updateSolveReadiness = () => {};
  app.commands.syncConfigToServer = async () => {};
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.getActivePalette = () => ["red"];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.showToast = (message, kind) => calls.push([kind, message]);
  await app.commands.handleStartSolve();
  assert.deepEqual(calls[0], ["/palette/validate", { palette: ["red"] }]);
  assert.equal(calls.some(([kind]) => kind === "start"), false);
  assert.equal(calls.some(([kind]) => kind === "error"), true);
  assert.equal(app.state.solve.solveStartPending, false);
});

test("suggestion and export cancellation validate captured job ownership", async () => {
  const calls = [];
  const { app } = await harness({ api: {
    apiPost: async (path) => {
      calls.push(path);
      return { cancelled: true, job_id: "suggest-1" };
    },
    cancelExport: async (jobId) => ({ cancelled: true, job_id: jobId }),
  } });
  app.commands.renderSuggestCancellationState = () => {};
  app.commands.renderExportCancellationState = () => {};
  app.commands.showToast = (message) => calls.push(message);
  app.state.ui._suggestPolling = { jobId: "suggest-1" };
  app.state.ui.activeSuggestJobId = "suggest-1";
  await app.commands.requestSuggestCancellation();
  assert.equal(app.state.palette.suggestCancelPending, true);
  assert.match(calls[0], /suggest\/cancel\?job_id=suggest-1/);

  app.state.export.exportRunning = true;
  app.state.export.activeExportJobId = "export-1";
  await app.commands.requestExportCancellation();
  assert.equal(app.state.export.exportCancelPending, true);
  assert.ok(calls.includes("Export cancellation requested"));
});
